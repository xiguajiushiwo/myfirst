"""识别 / 外观质检 服务层：跑通「识别 + 外观 + 读标签」、组装记录、算综合判定。

从 server.py 抽出的共享业务逻辑，供 recognition / cameras / pipeline 各 router 复用。
纯函数 + 落盘，不含 FastAPI 路由。
"""
from __future__ import annotations

import datetime
import os
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import UploadFile

from . import metrics
from .core import (ARCHIVE_DIR, IMG_EXT, OUTPUT_DIR, SLOT_KIND, SLOT_LABELS,
                   SPREAD_THRESHOLD_WEEKS, UPLOAD_DIR)
from .storage import db
from .recognition import template_store
from .recognition.region_ocr import (recognize_chip, recognize_pcb,
                                      recognize_rules, recognize_side)
from .recognition.geo_ocr import recognize_geo
from .recognition.date_parser import summarize, to_yyyyww
from .recognition.visualize import annotate_clean
from .recognition.barcode import read_label_code
from .inspection.quality_inspect import inspect_module, inspect_tray, read_label_vl


def _read_label(front_path):
    """读一根标签：**SN 只认二维码精确解**（zxing 带纠错，唯一可信来源）。

    二维码解出 → 用其 SN/品牌/型号/规格（src='barcode'，精确）。
    二维码解不出 → **SN 绝不采纳大模型猜测，留空待人工**；大模型只补 品牌/型号/频率
    作参考（src='vl'、sn_unread=True），因为 SN 是追溯/去重/防偷换的主键，猜错代价极大。
    """
    if not front_path:
        return {}
    code = read_label_code(front_path)
    if code.get("sn"):
        return code                                  # 二维码精确解，SN 可信
    vl = read_label_vl(front_path) or {}
    vl["sn"] = ""                                    # 二维码没解开 → SN 留空，不用大模型猜的
    vl["sn_unread"] = True                           # 标记：SN 未能精确读出，需人工
    vl["src"] = "vl"
    return vl

import datetime as _dt
import logging

log = logging.getLogger("yxq.services")


# --------------------- 追溯图片归档 ---------------------

_URL_ROOTS = {"/uploads": UPLOAD_DIR, "/outputs": OUTPUT_DIR, "/archive": ARCHIVE_DIR}


def _url_to_path(url: str):
    """把 /uploads|/outputs|/archive 的 URL 映射回磁盘路径；非法返回 None。"""
    if not url:
        return None
    for pre, root in _URL_ROOTS.items():
        if url.startswith(pre + "/"):
            return os.path.join(root, url[len(pre) + 1:].replace("/", os.sep))
    return None


def archive_record_images(record: dict, sub: str | None = None) -> dict:
    """把记录里引用的原图+标注图（/uploads、/outputs）**复制到永久归档区** `archive/<日期>/<sub>/`，
    并把 record 的 4 个图片字段改写成 /archive URL。uploads 被清理后仍可追溯。

    幂等：已是 /archive 的跳过；文件不存在的置空。
    """
    day = _dt.date.today().strftime("%Y%m%d")
    sub = sub or uuid.uuid4().hex[:12]
    dest_dir = os.path.join(ARCHIVE_DIR, day, sub)
    for field in ("front_img", "back_img", "annotated_front", "annotated_back"):
        url = (record.get(field) or "").strip()
        if url.startswith("/archive/"):
            continue
        src = _url_to_path(url)
        if src and os.path.isfile(src):
            os.makedirs(dest_dir, exist_ok=True)
            fn = os.path.basename(src)
            try:
                shutil.copyfile(src, os.path.join(dest_dir, fn))
                record[field] = f"/archive/{day}/{sub}/{fn}"
            except OSError as e:
                log.warning("归档图片失败 %s: %s", src, e)
                record[field] = ""
        else:
            record[field] = ""
    return record


def _images_from_rec(rec: dict) -> dict:
    """从 run_recognize 结果的 sides 里取正/背面的原图与标注图 URL。"""
    out = {"front_img": "", "back_img": "", "annotated_front": "", "annotated_back": ""}
    for s in rec.get("sides", []):
        if s.get("side") == "front":
            out["front_img"], out["annotated_front"] = s.get("image_url", ""), s.get("annotated_url", "")
        elif s.get("side") == "back":
            out["back_img"], out["annotated_back"] = s.get("image_url", ""), s.get("annotated_url", "")
    return out


# --------------------- 文件夹图片解析 ---------------------

def _match_slot(fname: str):
    n = fname.lower()
    if "front" in n or "正" in fname:
        return "front"
    if "back" in n or "背" in fname or "反" in fname:
        return "back"
    if "pcb" in n:
        return "pcb"
    if "controller" in n or "主控" in fname:
        return "controller"
    return None


def resolve_set(folder: str) -> dict:
    """把一个文件夹里的图片解析成 {slot: 绝对路径}。

    先按文件名关键字(front/back/pcb/controller/正/背)匹配；剩下的按 front→back→pcb→controller 顺序补。
    """
    try:
        imgs = sorted(f for f in os.listdir(folder)
                      if f.lower().endswith(IMG_EXT)
                      and os.path.isfile(os.path.join(folder, f)))
    except OSError:
        return {}
    slots, rest = {}, []
    for f in imgs:
        s = _match_slot(f)
        if s and s not in slots:
            slots[s] = os.path.join(folder, f)
        else:
            rest.append(os.path.join(folder, f))
    for p in rest:
        for s in ("front", "back", "pcb", "controller"):
            if s not in slots:
                slots[s] = p
                break
    return slots


# --------------------- 上传落盘 ---------------------

async def save_upload(uf: UploadFile, uid: str, slot: str) -> str:
    ext = os.path.splitext(uf.filename or "")[1].lower() or ".jpg"
    in_path = os.path.join(UPLOAD_DIR, f"{uid}_{slot}{ext}")
    with open(in_path, "wb") as f:
        f.write(await uf.read())
    return in_path


# --------------------- 统计 / 标注辅助 ---------------------

def _counts(codes):
    return {
        "dram": sum(c.code_type == "dram" for c in codes),
        "controller": sum(c.code_type == "controller" for c in codes),
        "pcb": sum(c.code_type == "pcb" for c in codes),
        "decoded": sum(1 for c in codes if c.week),
        "undecoded": sum(1 for c in codes if not c.week),
    }


def _make_title(slot, codes):
    label = SLOT_LABELS.get(slot, slot)
    if len(codes) == 1 and codes[0].week:          # 单芯片特写读出一个日期
        c = codes[0]
        return f"{label} · {c.year % 100}年{c.week}周"
    n = _counts(codes)
    parts = []
    if n["dram"]:
        parts.append(f"颗粒{n['dram']}")
    if n["controller"]:
        parts.append(f"主控{n['controller']}")
    if n["pcb"]:
        parts.append(f"PCB{n['pcb']}")
    tail = f" ({'+'.join(parts)})" if parts else ""
    return f"内存条{label} · {len(codes)}处日期{tail}"


def _week_ordinal(c):
    """把 (year, week) 折算成可比较的天序号（用该周周一）。"""
    if not c.week_start:
        return None
    try:
        return datetime.date.fromisoformat(c.week_start).toordinal()
    except ValueError:
        return None


# 几何模式（mode="geo"）下额外要算盲点的类型。
# 颗粒任何模式都算（防偷换的核心，每一颗都要看清）。而 PCB/PMIC/SOT 只在几何模式算 ——
# 几何模式对这三处**逐槽都出记录**，读不出必须转人工，否则"这处没检查过"和
# "这处检查合格"在结果里分不出来。老两条路（rules/template）的 PCB 读不出属既有行为，
# 不在本次改动范围内，避免动到已在产线验证过的判定口径。
_GEO_BLIND_TYPES = ("pcb", "pmic", "sot")


def _blind_dram(codes):
    """OCR 与大模型都没读出日期的框（无年/周）——可能藏着被换芯片的盲点，需人工确认。"""
    return [c for c in codes
            if not c.week and (c.code_type == "dram"
                               or (getattr(c, "_geo", False)
                                   and c.code_type in _GEO_BLIND_TYPES))]


def _loc_label(c):
    """异常定位标签：颗粒→'正面第3颗颗粒'，主控→'主控'，PCB→'PCB'。

    几何模式（`_geo`）多带一个槽号：那边 idx 是**逐槽**从 1 数的，
    不带槽号会出现四个"第3颗颗粒"，分不清是哪一根 —— 而铁律要求定位到具体那一颗。
    老两条路的标签保持原样，不动已验证的措辞。
    """
    where = ""
    if getattr(c, "_geo", False):
        slot = getattr(c, "slot", -1)
        if slot is not None and slot >= 0:
            where = f"第{slot + 1}根"
    if c.code_type == "dram":
        side = {"front": "正面", "back": "背面"}.get(getattr(c, "_side", ""), "")
        idx = getattr(c, "idx", None)
        return f"{side}{where}第{idx}颗颗粒" if idx else f"{side}{where}颗粒"
    name = {"controller": "主控", "pcb": "PCB", "pmic": "PMIC", "sot": "SOT"}.get(
        c.code_type, c.code_type)
    return f"{where}{name}"


# --------------------- 综合判定（逐颗，严禁多数表决）---------------------

def compute_signal(codes, threshold: float = SPREAD_THRESHOLD_WEEKS):
    """合格信号：所有能读出的日期【逐颗】比较，最大周差 ≤ threshold 才合格。

    **不做多数表决、不剔除离群**——因为个别颗粒可能是被人偷换过的芯片，必须能查出来：
    任何一颗日期与其余不一致（超差）即判不合格，并定位到具体是哪一颗。
    读不清/遮挡的颗粒是盲点（可能藏着被换芯片），单列出来提示人工确认。
    threshold 由前端传入（可调），缺省取 SPREAD_THRESHOLD_WEEKS。
    """
    thr = SPREAD_THRESHOLD_WEEKS if threshold is None else threshold
    thr_txt = f"{thr:g}"
    blind = _blind_dram(codes)
    blind_desc = [_loc_label(c) for c in blind]
    pairs = [(c, _week_ordinal(c)) for c in codes if c.week]
    pairs = [(c, o) for c, o in pairs if o is not None]
    if len(pairs) < 2:
        return {
            "status": "unknown", "qualified": None,
            "spread_weeks": 0.0, "threshold": thr,
            "count": len(pairs), "blind": len(blind), "blind_desc": blind_desc,
            "message": "有效日期不足 2 个，无法比较周差",
        }
    lo_c, lo = min(pairs, key=lambda p: p[1])
    hi_c, hi = max(pairs, key=lambda p: p[1])
    spread = round((hi - lo) / 7.0, 1)
    qualified = spread <= thr
    blind_txt = ("；另有 " + "、".join(blind_desc) + " 看不清日期，请人工确认") if blind else ""
    return {
        "status": "pass" if qualified else "fail",
        "qualified": qualified,
        "spread_weeks": spread,
        "threshold": thr,
        "count": len(pairs),
        "blind": len(blind),
        "blind_desc": blind_desc,
        "earliest": f"{lo_c.year}年{lo_c.week}周",
        "latest": f"{hi_c.year}年{hi_c.week}周",
        "message": (
            f"最大周差 {spread} 周 ≤ {thr_txt}，合格{blind_txt}"
            if qualified else
            f"最大周差 {spread} 周 > {thr_txt}，不合格（"
            f"最早 {_loc_label(lo_c)}{lo_c.year % 100}年{lo_c.week}周，"
            f"最晚 {_loc_label(hi_c)}{hi_c.year % 100}年{hi_c.week}周）{blind_txt}"
        ),
    }


def _structure_dates(all_codes, signal):
    """把识别结果整理成入库用的结构化日期字段（YYYYWW）+ 日期不合格说明。"""
    controller_date = pcb_date = ""
    storage_chips = []
    for c in all_codes:
        if c.code_type == "controller" and c.week and not controller_date:
            controller_date = to_yyyyww(c.year, c.week)
        elif c.code_type == "pcb" and c.week and not pcb_date:
            pcb_date = to_yyyyww(c.year, c.week)
        elif c.code_type == "dram":
            storage_chips.append({
                "idx": getattr(c, "idx", None),
                "side": getattr(c, "_side", ""),
                "yyyyww": to_yyyyww(c.year, c.week),
                "status": c.status,
            })
    storage_chips.sort(key=lambda x: (x["side"], x["idx"] or 0))

    date_ok = None
    date_fail = []
    if signal.get("status") == "pass":
        date_ok = True
    elif signal.get("status") == "fail":
        date_ok = False
        # 定位最早/最晚的部件（逐颗比较，任何一颗不一致都要能查出是哪颗）
        dated = [c for c in all_codes if c.week and c.week_start]
        try:
            key = lambda c: datetime.date.fromisoformat(c.week_start).toordinal()
            lo, hi = min(dated, key=key), max(dated, key=key)
            date_fail.append(
                f"日期超差 {signal.get('spread_weeks')} 周："
                f"最早 {_loc_label(lo)}({lo.year % 100}年{lo.week}周)，"
                f"最晚 {_loc_label(hi)}({hi.year % 100}年{hi.week}周)，"
                f"超过阈值 {signal.get('threshold')} 周")
        except (ValueError, TypeError):
            date_fail.append(signal.get("message", "日期不合格"))
    return {"controller_date": controller_date, "pcb_date": pcb_date,
            "storage_chips": storage_chips, "date_ok": date_ok, "date_fail": date_fail}


# --------------------- 核心识别 + 并行编排 ---------------------

def _recognize_core(paths: dict, uid: str, mode="rules", template_id=None,
                    current_year=None, threshold=None, vl_check=False) -> dict:
    """逐图识别 + 标注（含托盘空位跳过），返回**识别对象**与每面元数据。

    供 `run_recognize`（对外 JSON）与 `analyze_and_save`（按根拆分入库）共用——
    后者需要 DateCode 对象与每面槽位占位框，故此函数返回对象、不做 JSON 化。
    """
    mode = (mode or "rules").lower()
    sides_out, all_codes = [], []
    occ_by_side = {}
    ocr_sec = 0.0
    coverage_warn = []
    stick_total = 0

    def _one_side(slot, in_path):
        """跑一面：识别 + 标注。正/反面互不依赖，可并行（实测串行 9.0s → 并行 5.5s）。"""
        t_ocr = time.perf_counter()
        warns = []                                 # 本面的漏检告警（并行下不共享外层 list）
        occ = []                                   # 托盘槽位占位（仅整图固定模板面有）
        if slot == "pcb":
            codes = recognize_pcb(in_path, current_year=current_year)
        elif mode == "geo" and SLOT_KIND.get(slot) == "side":
            # 几何定位模式：框每张图现算，不吃模板坐标（不会滑框）。
            # 打 _geo 标记供 _blind_dram / _loc_label 区分 —— 这两处对几何模式
            # 口径不同（PCB/PMIC/SOT 也算盲点、定位标签带槽号）。
            loc = {}
            codes = recognize_geo(in_path, current_year=current_year, loc_out=loc)
            for c in codes:
                c._geo = True
            warns.extend((loc.get("warn") or []))
        elif mode == "rules":
            codes = recognize_rules(in_path, current_year=current_year, side=slot)
        elif SLOT_KIND.get(slot) == "side":
            codes = recognize_side(in_path, slot, current_year=current_year,
                                   template_id=template_id, occ_out=occ)
        else:
            codes = recognize_chip(in_path, slot, current_year=current_year)
        side_sec = time.perf_counter() - t_ocr

        for c in codes:
            c._side = slot
        title = _make_title(slot, codes)
        # 原图 URL：若在 uploads/ 下则用相对路径（含子文件夹），否则退回文件名
        rel = os.path.relpath(in_path, UPLOAD_DIR).replace("\\", "/")
        img_url = "/uploads/" + (rel if not rel.startswith("..") else os.path.basename(in_path))
        out_name = f"{uid}_{slot}_annotated.png"
        empty_slots = [o["box"] for o in occ if not o["occupied"]]   # 空槽 → 图上标「空位」
        annotate_clean(in_path, codes, os.path.join(OUTPUT_DIR, out_name),
                       title=title, empty_slots=empty_slots)
        stick_count = sum(1 for o in occ if o["occupied"]) if occ else 0
        side_out = {
            "side": slot, "kind": SLOT_KIND.get(slot, "side"),
            "label": SLOT_LABELS.get(slot, slot), "title": title,
            "image_url": img_url,
            "annotated_url": f"/outputs/{out_name}",
            "counts": _counts(codes), "codes": [c.to_dict() for c in codes],
            "slots": occ, "stick_count": stick_count,   # 占位概要 + 本面识别到的根数
        }
        # 整图大模型核对漏检（仅正/背面颗粒面）
        if vl_check and slot in ("front", "back"):
            from .inspection.quality_inspect import count_dram_vl
            cov = count_dram_vl(in_path)
            if cov:
                detected = sum(1 for c in codes if c.code_type == "dram")
                vl_total = cov.get("total", 0)
                missing = max(0, vl_total - detected)
                cov.update({"detected": detected, "missing": missing})
                side_out["coverage"] = cov
                if missing >= 2:                    # 明显偏少才提示（容忍 ±1 误差）
                    warns.append(
                        f"{SLOT_LABELS.get(slot, slot)}：OCR 检出 {detected} 颗，"
                        f"大模型看到约 {vl_total} 颗，疑似漏检 {missing} 颗，请人工核对")
        return slot, codes, occ, side_out, stick_count, side_sec, warns

    todo = [(slot, p) for slot, p in paths.items() if p]
    if len(todo) > 1:
        with ThreadPoolExecutor(max_workers=len(todo)) as ex:
            done = list(ex.map(lambda a: _one_side(*a), todo))
    else:
        done = [_one_side(*a) for a in todo]
    for slot, codes, occ, side_out, stick_count, side_sec, warns in done:
        all_codes.extend(codes)
        occ_by_side[slot] = occ
        stick_total = max(stick_total, stick_count)   # 一盘根数取正/背面较大值
        ocr_sec = max(ocr_sec, side_sec)              # 并行 → 取较慢面(墙钟)，不是相加
        coverage_warn.extend(warns)
        sides_out.append(side_out)
    return {
        "mode": mode, "all_codes": all_codes, "occ_by_side": occ_by_side,
        "sides": sides_out, "ocr_sec": round(ocr_sec, 2), "coverage_warn": coverage_warn,
        "stick_total": stick_total,
        "template_id": (template_id or template_store.default_template_id()) if mode == "template" else None,
    }


def _stick_breakdown(all_codes, threshold):
    """按槽位(每根)分组，**逐根**各自 compute_signal（同一根内逐颗比较，防跨根多批次误判）。

    返回 [{slot, pos, signal, dates, counts}]（JSON-safe），仅整图固定模板(有 slot)时非空。
    """
    slots_present = sorted({c.slot for c in all_codes if getattr(c, "slot", -1) >= 0})
    sticks = []
    for si in slots_present:
        sc = [c for c in all_codes if c.slot == si]
        sig = compute_signal(sc, threshold)
        sticks.append({"slot": si, "pos": si + 1, "signal": sig,
                       "dates": _structure_dates(sc, sig), "counts": _counts(sc)})
    return sticks


def run_recognize(paths: dict, uid: str, mode="rules", template_id=None,
                  current_year=None, threshold=None, vl_check=False) -> dict:
    """核心识别（对外 JSON）：逐图识别+标注+结构化。

    被 /api/recognize（手动上传）、/api/folder、/api/camera 共用。多根托盘时额外给 `sticks`
    （逐根判定）；顶层 signal/dates 仍为整图汇总（展示用，多根真值以 sticks 为准）。
    """
    core = _recognize_core(paths, uid, mode, template_id, current_year, threshold, vl_check)
    all_codes = core["all_codes"]
    signal = compute_signal(all_codes, threshold)
    return {
        "ok": True, "mode": core["mode"], "ocr_sec": core["ocr_sec"],
        "template_id": core["template_id"],
        "total": len(all_codes), "signal": signal,
        "stick_count": core["stick_total"],          # 本盘实际根数（占用槽数；0=非托盘模板模式）
        "sticks": _stick_breakdown(all_codes, threshold),   # 逐根判定（每根 signal/dates/counts）
        "dates": _structure_dates(all_codes, signal),
        "sides": core["sides"], "summary": summarize(all_codes),
        "coverage_warn": core["coverage_warn"],
    }


def analyze_all(paths: dict, uid: str, mode="rules", template_id=None,
                current_year=None, threshold=None, vl_check=False):
    """**并行**跑 识别(OCR+PCB大模型) / 外观质检 / 读标签，缩短总时长。

    三个任务相互独立：OCR 在本地(GPU)，外观质检与读标签是大模型网络调用；
    并发后墙钟时间 ≈ max(识别, 外观, 标签)，而非三者相加。
    返回 (recognize, inspect, label)，recognize['elapsed_sec'] 为并行后的总耗时。
    vl_check=True 时识别里额外做"整图大模型核对漏检"。
    """
    t0 = time.perf_counter()
    has_f, has_b = bool(paths.get("front")), bool(paths.get("back"))
    with ThreadPoolExecutor(max_workers=3) as ex:
        fut_rec = ex.submit(run_recognize, paths, uid, mode, template_id, current_year, threshold, vl_check)
        fut_insp = ex.submit(inspect_module, paths.get("front"), paths.get("back")) if (has_f or has_b) else None
        fut_label = ex.submit(_read_label, paths["front"]) if has_f else None
        rec = fut_rec.result()
        insp = fut_insp.result() if fut_insp else {}
        label = fut_label.result() if fut_label else {}
    rec["elapsed_sec"] = round(time.perf_counter() - t0, 2)
    return rec, insp, label


def build_record(rec: dict, insp: dict, label: dict, operator: str,
                 batch: dict | None = None) -> dict:
    """把识别+质检+标签 组装成一条数据库记录（含综合判定与不合格说明）。

    batch 给定时（当前批次登记信息）：品牌/容量/频率/客户/批次号**以批次登记为准**，
    每根只用标签里的 SN；未给 batch 时沿用标签读出的品牌/型号/频率（旧行为）。
    """
    dates = rec.get("dates", {}) or {}
    date_ok = dates.get("date_ok")
    ins_ok = bool(insp.get("ok"))
    comp_ok = insp.get("comp_ok") if ins_ok else None
    gf_ok = insp.get("gold_finger_ok") if ins_ok else None
    cm_ok = insp.get("chip_mark_ok") if ins_ok else None
    fails = list(dates.get("date_fail") or []) + list(insp.get("appearance_fails") or [])
    checks = [date_ok, comp_ok, gf_ok, cm_ok]
    if any(c is False for c in checks):
        verdict = "fail"
    elif all(c is True for c in checks):
        verdict = "pass"
    else:
        verdict = "unknown"
    lb = label or {}
    b = batch or {}
    # 批次登记为准：有批次则品牌/容量/频率/客户/批次号取批次，否则回退标签读数（含二维码解码结果）
    brand = b.get("brand") or lb.get("brand", "")
    frequency = b.get("frequency") or lb.get("frequency", "")
    # 型号：订单登记优先，回退标签读数；供应商仅订单登记有（标签读不出）
    model = b.get("model") or lb.get("model", "")
    supplier = b.get("supplier", "")
    # SN 是追溯/去重/防偷换主键：二维码没解出(SN 空) → 不能静默判合格，降级为「待人工补录」
    sn_missing = not (lb.get("sn") or "").strip()
    if sn_missing:
        if verdict == "pass":
            verdict = "unknown"
        fails.append("SN 二维码未解出，待人工补录（SN 只认二维码，不采纳大模型猜测）")
    return {
        "operator": operator or "",
        "sn": lb.get("sn", ""), "brand": brand,
        "model": model, "frequency": frequency,
        "spec": lb.get("spec", ""), "mfg": lb.get("mfg", ""),
        "customer": b.get("customer", ""), "supplier": supplier,
        "capacity": b.get("capacity") or lb.get("capacity", ""),
        "batch_no": b.get("batch_no", ""), "cond": b.get("cond", ""),
        "remark": b.get("remark", ""),
        "controller_date": dates.get("controller_date") or None,
        "pcb_date": dates.get("pcb_date") or None,
        "storage_chips": dates.get("storage_chips") or [],
        "date_ok": date_ok, "comp_ok": comp_ok,
        "gold_finger_ok": gf_ok, "chip_mark_ok": cm_ok,
        "verdict": verdict,
        "fail_desc": ("；".join(fails) if (verdict != "pass" and fails) else ""),
        "review_status": "未复查",
        "sn_unread": sn_missing,                     # SN 未精确读出(二维码没解开)→ 前端标红待人工
        "label_data": {
            key: lb.get(key) for key in
            ("sn", "model", "brand", "spec", "mfg", "capacity", "frequency",
             "raw", "src", "sn_unread") if lb.get(key) not in (None, "")
        },
    }


def _crop_slot(src_path, box_px, out_tag) -> str | None:
    """把某槽区域从原图裁出存到 uploads（供该根**单独**送大模型读 SN / 外观）。

    box_px = [x0,y0,x1,y1]（像素）。无路径/无框/裁空 → 返回 None。
    """
    if not src_path or not box_px:
        return None
    try:
        from PIL import Image
        img = Image.open(src_path).convert("RGB")
        W, H = img.size
        x0, y0, x1, y1 = box_px
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(W, int(x1)), min(H, int(y1))
        if x1 <= x0 or y1 <= y0:
            return None
        out = os.path.join(UPLOAD_DIR, f"{out_tag}.png")
        img.crop((x0, y0, x1, y1)).save(out)
        return out
    except Exception:
        return None


def _stick_summary(record: dict, rid) -> dict:
    """一根记录 → SSE/接口用的精简结果。"""
    return {
        "slot_pos": record.get("slot_pos"), "record_id": rid,
        "verdict": record["verdict"], "sn": record["sn"], "brand": record["brand"],
        "model": record.get("model", ""), "frequency": record.get("frequency", ""),
        "spec": record.get("spec", ""), "mfg": record.get("mfg", ""),
        "label_data": record.get("label_data") or {},
        "sn_unread": record.get("sn_unread", False),
        "controller_date": record["controller_date"], "pcb_date": record["pcb_date"],
        "storage_count": len(record["storage_chips"]), "fail_desc": record["fail_desc"],
        # 外观质检逐项（前端要分项显示：日期一致/元器件/金手指/芯片打磨）
        # True=合格 False=不合格 None=未检(大模型不可用)
        "date_ok": record.get("date_ok"), "comp_ok": record.get("comp_ok"),
        "gold_finger_ok": record.get("gold_finger_ok"),
        "chip_mark_ok": record.get("chip_mark_ok"),
        "inspection_id": record.get("inspection_id", ""),
        "recognition_mode": record.get("recognition_mode", "rules"),
        "timing": record.get("timing") or {},
        "elapsed_sec": record.get("elapsed_sec") or 0,
        "token_usage": record.get("token_usage") or {},
    }


# --------------------- 防重复放盘（本批已见指纹）---------------------

_SEEN_TRAYS: dict = {}     # {batch_id: set(fingerprint)}；登记新批次天然是新 key，隔天合法复检不受影响


def tray_fingerprint(sns) -> str | None:
    """一盘的指纹 = 非空 SN 去重排序拼接。有效 SN < 2 返回 None（不去重，避免空 SN 误判为重复）。"""
    clean = sorted({(s or "").strip() for s in (sns or []) if (s or "").strip()})
    return "|".join(clean) if len(clean) >= 2 else None


def _is_dup_tray(batch_id, fp) -> bool:
    """本批是否已见过该指纹；未见则记下。无 batch_id / 无指纹 → 不算重复。"""
    if not batch_id or not fp:
        return False
    seen = _SEEN_TRAYS.setdefault(batch_id, set())
    if fp in seen:
        return True
    seen.add(fp)
    return False


# --------------------- 同 SN 历史比对（防偷换第二道）---------------------

def _rec_date_sig(rec: dict) -> tuple:
    """一条记录的日期签名：颗粒日期(YYYYWW)集合 + 主控 + PCB，用于跨次比对。"""
    chips = rec.get("storage_chips") or []
    drams = tuple(sorted(c.get("yyyyww", "") for c in chips
                         if isinstance(c, dict) and c.get("yyyyww")))
    return (drams, rec.get("controller_date") or "", rec.get("pcb_date") or "")


def sn_history_diff(records: list[dict]) -> dict:
    """同一 SN 多次质检的日期是否变了（疑似中途被换过芯片）。

    records：该 SN 的历次记录（任意序）。若不同次的**颗粒日期集合/主控/PCB** 不一致 → changed=True。
    """
    sigs = [_rec_date_sig(r) for r in records]
    changed = len({s for s in sigs}) > 1
    return {
        "count": len(records),
        "changed": changed,
        "message": ("⚠ 同一 SN 历次质检的日期码不一致，疑似中途被换过芯片，请人工核查"
                    if changed else "历次日期一致"),
    }


def _slot_template(template_id=None):
    """选择托盘几何模板；规则识别只借用槽位坐标，不借用日期框。"""
    if template_id:
        requested = template_store.get_template(template_id)
        sides = (requested or {}).get("sides") or {}
        front_n = len((sides.get("front") or {}).get("slots") or [])
        back_n = len((sides.get("back") or {}).get("slots") or [])
        if front_n >= 4 and back_n >= 4:
            return template_id, requested
        log.warning("定位模板 %s 缺少正反面四槽坐标，改用完整四槽模板", template_id)
    candidates = []
    for item in template_store.list_templates():
        tpl = template_store.get_template(item.get("id"))
        sides = (tpl or {}).get("sides") or {}
        front_n = len((sides.get("front") or {}).get("slots") or [])
        back_n = len((sides.get("back") or {}).get("slots") or [])
        candidates.append((min(front_n, back_n), front_n + back_n, item.get("id"), tpl))
    if not candidates:
        return None, None
    _, _, tid, tpl = max(candidates, key=lambda row: (row[0], row[1]))
    return tid, tpl


def _pre_crops(paths: dict, uid: str, mode: str, template_id):
    """用托盘槽位几何检测占用并裁出每根正反图；日期识别仍始终走规则模式。"""
    t0 = time.perf_counter()
    try:
        from PIL import Image
        from .recognition.region_ocr import detect_occupied_slots, _slot_rects_for_layout

        tid, tpl = _slot_template(template_id)
        if not tpl:
            return None
        boxes, occ_slots, configured_slots, occupancy = {}, set(), set(), {}
        for side in ("front", "back"):
            p = paths.get(side)
            layout = ((tpl.get("sides") or {}).get(side)) if p else None
            if not layout:
                continue
            rects = _slot_rects_for_layout(layout)
            if not rects:
                continue
            occ = detect_occupied_slots(Image.open(p).convert("RGB"), rects)
            occupancy[side] = occ
            for o in occ:
                configured_slots.add(o["slot"])
                boxes[(side, o["slot"])] = o["box"]
                if o["occupied"]:
                    occ_slots.add(o["slot"])
        if not configured_slots:
            return None
        crops = {
            si: (_crop_slot(paths.get("front"), boxes.get(("front", si)), f"{uid}_s{si+1}_front"),
                 _crop_slot(paths.get("back"), boxes.get(("back", si)), f"{uid}_s{si+1}_back"))
            for si in sorted(occ_slots)
        }
        return {
            "template_id": tid,
            "configured_slots": sorted(configured_slots),
            "occupied_slots": sorted(occ_slots),
            "boxes": boxes,
            "occupancy": occupancy,
            "crops": crops,
            "elapsed_sec": round(time.perf_counter() - t0, 3),
        }
    except Exception as e:  # noqa: BLE001
        log.info("托盘槽位检测/裁图失败：%s", e)
        return None


def _vl_branch(crops: dict, stage=None):
    """逐根并行跑外观质检与标签解码，分类耗时均取最慢任务的墙钟时间。"""
    slots = sorted(crops)
    if stage:
        try:
            stage("inspect", f"已裁出 {len(slots)} 根，大模型外观质检 + 读二维码进行中（与日期识别并行）…")
        except Exception:  # noqa: BLE001
            pass
    t = time.perf_counter()

    def _insp(si):
        started = time.perf_counter()
        f, b = crops[si]
        result = inspect_module(f, b) if (f or b) else {}
        return result, time.perf_counter() - started

    def _lbl(si):
        started = time.perf_counter()
        f, _ = crops[si]
        result = _read_label(f)
        return result, time.perf_counter() - started

    with ThreadPoolExecutor(max_workers=min(8, 2 * len(slots))) as ex:
        fi = {si: ex.submit(_insp, si) for si in slots}
        fl = {si: ex.submit(_lbl, si) for si in slots}
        insp_done = {si: fi[si].result() for si in slots}
        label_done = {si: fl[si].result() for si in slots}
    insps = {si: value[0] for si, value in insp_done.items()}
    lbls = {si: value[0] for si, value in label_done.items()}
    timing = {
        "appearance": round(max((value[1] for value in insp_done.values()), default=0), 3),
        "label_decode": round(max((value[1] for value in label_done.values()), default=0), 3),
        "inspect_label_parallel": round(time.perf_counter() - t, 3),
    }
    return insps, lbls, timing, crops


def _assign_rule_codes_to_slots(core: dict, slot_info: dict) -> None:
    """按日期框中心点落入的托盘槽位，将整图规则 OCR 结果归到每根内存条。

    **几何模式的读数不在此重排**：`recognize_geo` 已按每张图现算的条带定好槽号，
    再拿模板坐标覆盖一遍等于把"会滑框"重新引回来 —— 框一滑，某颗颗粒的日期就会
    被记到隔壁那根头上，正是铁律要防的漏判。所以 `_geo` 标记过的读数保留原槽号，
    只做一次一致性核对：几何数出的根数与占位检测不一致就出告警转人工，不猜。
    """
    boxes = slot_info.get("boxes") or {}
    occupied = set(slot_info.get("occupied_slots") or [])
    geo_codes = [c for c in (core.get("all_codes") or []) if getattr(c, "_geo", False)]
    if geo_codes:
        geo_slots = sorted({c.slot for c in geo_codes if getattr(c, "slot", -1) >= 0})
        if occupied and len(geo_slots) != len(occupied):
            core.setdefault("coverage_warn", []).append(
                f"几何定位数出 {len(geo_slots)} 根、托盘占位检测数出 {len(occupied)} 根，"
                f"两者不一致，槽位对应可能有误，请人工核对")
        elif occupied and geo_slots != sorted(occupied):
            # 根数相同但编号不同（例：中间空一槽 → 几何编 0,1,2 而占位编 0,1,3）。
            # 按左→右次序一一对上即可，两边都是同一物理顺序。
            remap = dict(zip(geo_slots, sorted(occupied)))
            for c in geo_codes:
                if c.slot in remap:
                    c.slot = remap[c.slot]
    for code in core.get("all_codes") or []:
        if getattr(code, "_geo", False):
            continue
        side = getattr(code, "_side", "")
        poly = getattr(code, "box", None) or []
        candidates = [(si, box) for (box_side, si), box in boxes.items()
                      if box_side == side and (not occupied or si in occupied)]
        if not poly or not candidates:
            continue
        cx = sum(float(p[0]) for p in poly) / len(poly)
        cy = sum(float(p[1]) for p in poly) / len(poly)
        inside = [si for si, (x0, y0, x1, y1) in candidates
                  if x0 <= cx <= x1 and y0 <= cy <= y1]
        if inside:
            code.slot = inside[0]
        else:
            code.slot = min(candidates,
                            key=lambda row: abs(cx - (row[1][0] + row[1][2]) / 2))[0]
    core["occ_by_side"] = slot_info.get("occupancy") or {}
    core["stick_total"] = len(occupied)


def analyze_and_save(job, operator="", mode="rules", template_id=None,
                     current_year=None, threshold=None, batch_id=None, save=True,
                     on_stage=None) -> dict:
    """手动处理一盘：四槽几何裁图 + 规则 OCR + 逐根标签/外观 + 入库。"""
    t0 = time.perf_counter()
    uid = uuid.uuid4().hex[:12]
    # 本工作流只认 rules / geo 两种：
    #   rules —— 原有行为，整图找日期、不拆槽、读不到 PCB（默认，一行未动）
    #   geo   —— 几何定位，框每张图现算，逐槽出 颗粒 + PCB/PMIC/SOT
    # template 模式不在此工作流启用（它吃固定坐标会滑框，且这里的 template_id
    # 本来只用于托盘槽位几何裁图，不参与识别）。传别的值一律落回 rules。
    mode = "geo" if (mode or "").lower() == "geo" else "rules"
    token_before = metrics.vl_usage()
    paths = {}
    for slot, src in (job.paths or {}).items():
        if src and os.path.isfile(src):
            # 双相机采集已把原图存进 uploads/<序号>/front|back.jpg —— 直接就地用，
            # 不再复制成扁平 uploads/{uid}_{slot}.jpg，保持 uploads/ 根目录整洁。
            if os.path.abspath(src).startswith(os.path.abspath(UPLOAD_DIR)):
                paths[slot] = src
            else:
                dst = os.path.join(UPLOAD_DIR, f"{uid}_{slot}{os.path.splitext(src)[1].lower()}")
                shutil.copyfile(src, dst)
                paths[slot] = dst
    if not paths:
        return {"ok": False, "pos_id": job.pos_id, "error": "无有效图片"}
    prepare_sec = round(time.perf_counter() - t0, 3)

    def _stage(name, text, **kw):
        if on_stage:
            try:
                on_stage(name, text, **kw)
            except Exception:  # noqa: BLE001
                pass

    pre = _pre_crops(paths, uid, mode, template_id)
    crop_sec = (pre or {}).get("elapsed_sec", 0.0)
    occupied = (pre or {}).get("occupied_slots") or []
    if pre and not occupied:
        return {
            "ok": False, "pos_id": job.pos_id, "error": "未检测到托盘中的内存条，请检查摆放或槽位标定",
            "recognition_mode": mode,
            "timing": {"file_prepare": prepare_sec, "slot_detect_crop": crop_sec,
                       "total": round(time.perf_counter() - t0, 3)},
            "token_usage": metrics.vl_usage_delta(token_before),
        }

    # 规则 OCR 与“逐根外观 + 标签解码”并行；parallel_analysis 是这一段唯一计入总链路的墙钟耗时。
    analysis_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as ex:
        future_vl = ex.submit(_vl_branch, pre["crops"], _stage) if pre else None
        rec_started = time.perf_counter()
        core = _recognize_core(paths, uid, mode, None, current_year, threshold)
        rec_sec = round(time.perf_counter() - rec_started, 3)
        if pre:
            _assign_rule_codes_to_slots(core, pre)
        _n_codes = len(core.get("all_codes") or [])
        _stage("inspect", f"规则 OCR 完成({rec_sec}s，读出 {_n_codes} 处日期)，等待并行任务…",
               recognize=rec_sec, codes=_n_codes)
        vl_done = future_vl.result() if future_vl else None
    parallel_sec = round(time.perf_counter() - analysis_started, 3)

    batch = db.get_batch(batch_id) if batch_id else None
    imgs = _images_from_rec({"sides": core["sides"]})     # 整盘原图/标注图（多根共用）
    all_codes = core["all_codes"]
    annotated = {s["side"]: s["annotated_url"] for s in core["sides"]}

    if pre and vl_done:
        occ_slots = occupied
        insps, lbls, vl_timing, _ = vl_done
        _stage("annotate", f"并行分析完成({parallel_sec}s)，生成标注图并{'入库' if save else '汇总'}…",
               parallel_analysis=parallel_sec, sticks=len(occ_slots))
        token_usage = metrics.vl_usage_delta(token_before)
        timing = {
            "file_prepare": prepare_sec,
            "slot_detect_crop": crop_sec,
            "rule_ocr": rec_sec,
            **vl_timing,
            "parallel_analysis": parallel_sec,
            "archive": 0.0,
            "database": 0.0,
        }

        sns = [(lbls.get(si) or {}).get("sn", "") for si in occ_slots]
        fp = tray_fingerprint(sns)
        if save and _is_dup_tray(batch_id, fp):
            timing["total"] = round(time.perf_counter() - t0, 3)
            return {
                "ok": True, "pos_id": job.pos_id, "multi": True, "duplicate": True,
                "inspection_id": uid, "recognition_mode": mode,
                "stick_count": len(occ_slots),
                "sn": "、".join(s for s in sns if s),
                "sticks": [{"slot_pos": si + 1, "sn": (lbls.get(si) or {}).get("sn", ""),
                            "sn_unread": not bool((lbls.get(si) or {}).get("sn")),
                            "label_data": lbls.get(si) or {}, "verdict": "unknown"}
                           for si in occ_slots],
                "annotated": annotated,
                "message": "本批已测过同样的一盘（SN 一致），已跳过、未重复入库",
                "elapsed_sec": timing["total"], "timing": timing, "token_usage": token_usage,
            }

        records = []
        for si in occ_slots:
            sc = [c for c in all_codes if getattr(c, "slot", -1) == si]
            sig = compute_signal(sc, threshold)
            rec_like = {"dates": _structure_dates(sc, sig), "sides": core["sides"]}
            record = build_record(rec_like, insps.get(si) or {}, lbls.get(si) or {}, operator, batch=batch)
            record.update(imgs)                            # 追溯：共用整盘原图/标注图
            record["slot_pos"] = si + 1                    # 托盘第几槽(1..N，左→右)
            record.update({"inspection_id": uid, "recognition_mode": mode,
                           "timing": timing, "token_usage": token_usage})
            records.append(record)

        archive_started = time.perf_counter()
        if save:
            for record in records:
                si = int(record["slot_pos"]) - 1
                archive_record_images(record, sub=f"{uid}_s{si+1}")
        timing["archive"] = round(time.perf_counter() - archive_started, 3) if save else 0.0

        ids = []
        db_started = time.perf_counter()
        for record in records:
            rid = None
            if save:
                try:
                    rid = db.save_record(record)
                except Exception as e:  # noqa: BLE001
                    record["_db_error"] = str(e)
                    log.exception("入库失败 pos=%s slot=%s", job.pos_id, record.get("slot_pos"))
            ids.append(rid)
        timing["database"] = round(time.perf_counter() - db_started, 3) if save else 0.0
        timing["total"] = round(time.perf_counter() - t0, 3)
        for record in records:
            record["timing"] = timing
            record["elapsed_sec"] = timing["total"]
        if save:
            db.update_record_runtime(ids, timing, token_usage, timing["total"])
        sticks_out = [_stick_summary(record, rid) for record, rid in zip(records, ids)]

        verdicts = [s["verdict"] for s in sticks_out]
        agg = ("fail" if "fail" in verdicts
               else "pass" if verdicts and all(v == "pass" for v in verdicts) else "unknown")
        fails = [f"第{s['slot_pos']}槽{('('+s['sn']+')') if s['sn'] else ''}：{s['fail_desc']}"
                 for s in sticks_out if s["verdict"] == "fail" and s["fail_desc"]]
        return {
            "ok": True, "pos_id": job.pos_id, "multi": True,
            "inspection_id": uid, "recognition_mode": mode,
            "stick_count": len(sticks_out), "sticks": sticks_out,
            "verdict": agg,
            "sn": "、".join(s["sn"] for s in sticks_out if s["sn"]),
            "storage_count": sum(s["storage_count"] for s in sticks_out),
            "fail_desc": "；".join(fails),
            "annotated": annotated,
            "elapsed_sec": timing["total"], "timing": timing, "token_usage": token_usage,
        }

    # 没有可用托盘几何时保留单根兼容路径，但仍使用规则 OCR。
    signal = compute_signal(all_codes, threshold)
    rec_like = {"dates": _structure_dates(all_codes, signal), "sides": core["sides"]}
    has_f, has_b = bool(paths.get("front")), bool(paths.get("back"))
    branch_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_insp = ex.submit(inspect_module, paths.get("front"), paths.get("back")) if (has_f or has_b) else None
        fut_label = ex.submit(_read_label, paths["front"]) if has_f else None
        insp = fut_insp.result() if fut_insp else {}
        label = fut_label.result() if fut_label else {}
    branch_sec = round(time.perf_counter() - branch_started, 3)
    token_usage = metrics.vl_usage_delta(token_before)
    record = build_record(rec_like, insp, label, operator, batch=batch)
    record.update(imgs)
    timing = {"file_prepare": prepare_sec, "slot_detect_crop": crop_sec,
              "rule_ocr": rec_sec, "inspect_label_parallel": branch_sec,
              "parallel_analysis": parallel_sec, "archive": 0.0, "database": 0.0}
    record.update({"inspection_id": uid, "recognition_mode": mode,
                   "timing": timing, "token_usage": token_usage})
    rid = None
    if save:
        started = time.perf_counter()
        archive_record_images(record, sub=uid)
        timing["archive"] = round(time.perf_counter() - started, 3)
        started = time.perf_counter()
        try:
            rid = db.save_record(record)
        except Exception as e:  # noqa: BLE001
            record["_db_error"] = str(e)
            log.exception("入库失败 pos=%s sn=%s", job.pos_id, record.get("sn"))
        timing["database"] = round(time.perf_counter() - started, 3)
    timing["total"] = round(time.perf_counter() - t0, 3)
    record["timing"], record["elapsed_sec"] = timing, timing["total"]
    if save and rid:
        db.update_record_runtime([rid], timing, token_usage, timing["total"])
    out = _stick_summary(record, rid)
    out.update({"ok": True, "pos_id": job.pos_id, "annotated": annotated,
                "inspection_id": uid, "recognition_mode": mode,
                "elapsed_sec": timing["total"], "timing": timing, "token_usage": token_usage})
    return out
