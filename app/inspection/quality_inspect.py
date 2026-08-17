"""内存条外观质检（通义千问 Qwen-VL 多模态大模型）。

把上传的正/背面照片直接交给 Qwen-VL，检查两类问题：

  第一部分 · 元器件外观
    - 电子元器件是否损坏 / 缺失 / 裂痕；
    - 是否有发黑 / 烧焦 / 变色；
    - 金手指（板卡底部金黄触点）是否正常。

  第二部分 · 存储芯片二维码标记
    - 每颗存储芯片表面有一个激光打标的小方块（类似二维码 / DataMatrix）；
    - 内部有清晰「线条 / 纹理」= 正常；只有一些散点而没有线条 = 异常。

判定：任一异常 → 红灯（不合格），并给出原因；全部正常 → 绿灯（合格）。

模型走 DashScope（阿里云百炼）OpenAI 兼容接口，默认用可用的最佳多模态模型。
密钥优先取环境变量 DASHSCOPE_API_KEY。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import time

import requests


def _load_dotenv():
    """读取项目根目录的 .env，把 KEY=VALUE 注入环境变量（不覆盖已存在的真实环境变量）。

    免依赖的极简实现：API 密钥等放在 .env 里集中管理，源码中不再硬编码。
    从本文件所在目录**逐级向上**查找 .env（与模块在包内层级无关，挪目录也不会坏）。
    """
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        path = os.path.join(d, ".env")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k:
                        os.environ.setdefault(k, v)
            return
        d = os.path.dirname(d)


_load_dotenv()

# 多模态大模型配置改由 settings_store 提供（当前启用的 provider），支持多模型切换；
# 未配置时回退 .env（QWEN_*/DASHSCOPE_*）。
import logging  # noqa: E402

from .. import settings_store, metrics, prompts  # noqa: E402

log = logging.getLogger("yxq.vl")


def _prov() -> dict:
    try:
        return settings_store.active_provider() or {}
    except Exception:
        return {}


def _base() -> str:
    return (_prov().get("base_url")
            or os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"))


def _timeout() -> int:
    return int(_prov().get("timeout") or os.environ.get("QWEN_TIMEOUT", "120"))


def _model() -> str:
    """当前启用的多模态模型名。"""
    return _prov().get("model") or os.environ.get("QWEN_VL_MODEL", "qwen3-vl-235b-a22b-instruct")


def _api_key() -> str:
    """当前 provider 的 API Key；回退 .env 的 DASHSCOPE_API_KEY。"""
    return (_prov().get("api_key") or os.environ.get("DASHSCOPE_API_KEY", "")).strip()

# 提示词统一放 app/prompts.py（调词只改那个文件）
_PROMPT = prompts.APPEARANCE


def _data_url(path: str) -> str:
    """PNG→JPEG(q92) 再 base64 上传：体积降到约 1/5、避开大图上传时的 SSL EOF，
    **不缩放**（长边 ≤ QWEN_MAX_SIDE=2048，对逐根裁图≈原样），最大限度保留芯片二维码/金手指
    等细节，避免影响大模型识别。实测(全合格样)各档判定一致；为稳妥仍按近原分辨率传。
    压缩失败(非常规图)回退原图字节，保证不因压缩坏了质检。"""
    try:
        import io
        from PIL import Image
        max_side = int(os.environ.get("QWEN_MAX_SIDE", "2048") or 2048)
        im = Image.open(path)
        im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > max_side:                       # 仅裁掉超大整图的冗余，逐根裁图基本不缩
            s = max_side / max(w, h)
            im = im.resize((max(1, int(w * s)), max(1, int(h * s))))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=92)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001
        mime = mimetypes.guess_type(path)[0] or "image/jpeg"
        b = base64.b64encode(open(path, "rb").read()).decode()
        return f"data:{mime};base64,{b}"


def _extract_json(text: str) -> dict:
    """从模型回复里抽出 JSON 对象（容忍 ```json 包裹或前后多余文字）。"""
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    # 退而求其次：截取第一个 { 到最后一个 }
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        return json.loads(t[i:j + 1])
    raise ValueError("模型未返回可解析的 JSON")


def _chat(content: list) -> str:
    """向多模态大模型发一次 chat/completions，**带降级容错**。

    按 settings_store.ordered_providers()（当前启用排第一，其余跟随）**逐个尝试**：
    某个 provider 网络/欠费/超时/非200/异常 → 记日志并**自动切下一个**；全部失败才抛错。
    成功即累计用量并返回文本。这样"第一个模型不行就用第二个"，即便现在只配了一个也无副作用。
    """
    providers = []
    try:
        providers = settings_store.ordered_providers()
    except Exception:
        providers = []
    if not providers:
        raise RuntimeError("未配置任何大模型（请在 系统设置 里添加）")

    last_err = "无可用模型"
    for p in providers:
        name = p.get("name") or p.get("id") or "?"
        key = (p.get("api_key") or "").strip() or os.environ.get("DASHSCOPE_API_KEY", "").strip()
        base = (p.get("base_url") or os.environ.get("QWEN_BASE_URL", "")).rstrip("/")
        model = p.get("model") or os.environ.get("QWEN_VL_MODEL", "")
        if not (key and base and model):
            last_err = f"{name} 配置不全(缺 key/base_url/model)"
            log.warning("大模型[%s] 跳过：%s", name, last_err)
            continue
        try:
            body = {"model": model, "messages": [{"role": "user", "content": content}], "temperature": 0.0}
            r = requests.post(f"{base}/chat/completions",
                              headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                              json=body, timeout=int(p.get("timeout") or 120))
            if r.status_code != 200:
                last_err = f"{name} [{r.status_code}] {r.text[:150]}"
                log.warning("大模型[%s] 调用失败，降级下一个：%s", name, last_err)
                continue
            resp = r.json()
            metrics.add_vl_usage(resp.get("usage"))
            return resp["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last_err = f"{name} {type(e).__name__}: {e}"
            log.warning("大模型[%s] 异常，降级下一个：%s", name, e)
            continue
    raise RuntimeError(f"所有大模型均不可用：{last_err}")


def _call_qwen(image_paths: list[str], prompt: str) -> str:
    """文本 + 若干整图 → 大模型（走 _chat 降级容错）。"""
    content = [{"type": "text", "text": prompt}]
    for p in image_paths:
        content.append({"type": "image_url", "image_url": {"url": _data_url(p)}})
    return _chat(content)


_PCB_DATE_PROMPT = prompts.PCB_DATE


def read_yyww_vl(image_path: str) -> tuple[str, str, float]:
    """用 Qwen-VL 从 PCB/芯片特写里读 YYWW 日期数字 + 大模型自评置信度。

    返回 (digits4, raw_text, confidence)：digits4 为 4 位数字串（读不出为空串），
    confidence 为大模型自评 0~1。任何异常（缺 key / 网络）都返回 ('', 错误说明, 0.0)，让调用方优雅回退。
    """
    try:
        ans = _call_qwen([image_path], _PCB_DATE_PROMPT)
        obj = _extract_json(ans)
        digits = re.sub(r"\D", "", str(obj.get("digits", "")))
        raw = str(obj.get("raw", "")).strip()
        try:
            conf = max(0.0, min(1.0, float(obj.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        return (digits if len(digits) == 4 else ""), raw, conf
    except Exception as e:
        return "", f"大模型读取失败：{e}", 0.0


_LABEL_PROMPT = prompts.LABEL


def read_label_vl(image_path: str) -> dict:
    """用 Qwen-VL 读正面标签，返回 {brand, model, frequency, sn}。异常返回全空。"""
    empty = {"brand": "", "model": "", "frequency": "", "sn": ""}
    try:
        obj = _extract_json(_call_qwen([image_path], _LABEL_PROMPT))
        return {k: str(obj.get(k, "") or "").strip() for k in empty}
    except Exception:
        return empty


def _img_data_url(pil_img) -> str:
    import io
    buf = io.BytesIO()
    pil_img.convert("RGB").save(buf, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _extract_json_array(text: str) -> list:
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        v = json.loads(t)
        return v if isinstance(v, list) else [v]
    except Exception:
        i, j = t.find("["), t.rfind("]")
        if i >= 0 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                return []
        return []


def read_crops_vl(crops: list, kind: str = "dram") -> list[str]:
    """批量把多张芯片日期码小图交给 Qwen-VL 识别，返回与输入等长的数字串列表。

    crops: list[PIL.Image]（每颗芯片的日期区域裁剪）。kind=dram(3位YWW)/其他(4位YYWW)。
    一次调用读全部，省时省钱；异常/缺 key 返回全空串。
    """
    crops = [c for c in crops if c is not None]
    if not crops:
        return []
    n = len(crops)
    prompt = prompts.crops(n, prompts.want_text(kind))
    content = [{"type": "text", "text": prompt}]
    for im in crops:
        content.append({"type": "image_url", "image_url": {"url": _img_data_url(im)}})
    out = [""] * n
    try:
        arr = _extract_json_array(_chat(content))   # _chat 内含降级容错
        for item in arr:
            try:
                i = int(item.get("i", 0))
            except (ValueError, TypeError):
                continue
            if 1 <= i <= n:
                out[i - 1] = re.sub(r"\D", "", str(item.get("digits", "")))
    except Exception:
        pass
    return out


def read_crop_vl(crop, kind: str = "dram") -> str:
    """把【单颗】芯片日期码小图交给 Qwen-VL 识别，返回数字串（读不出为空串）。

    单图单调用，比批量拼多图更准（不会串行错位/相互干扰），用于低置信/未读颗粒的逐颗兜底。
    kind=dram(3位YWW)/其他(4位YYWW)。异常/缺 key 返回空串。
    """
    if crop is None:
        return ""
    prompt = prompts.crop(prompts.want_text(kind))
    content = [{"type": "text", "text": prompt},
               {"type": "image_url", "image_url": {"url": _img_data_url(crop)}}]
    try:
        obj = _extract_json(_chat(content))         # _chat 内含降级容错
        return re.sub(r"\D", "", str(obj.get("digits", "")))
    except Exception:
        return ""


def count_dram_vl(image_path: str) -> dict:
    """整图数颗粒 + 日期分布（防漏检核对用）。返回 {total, dist, unclear, note} 或 {}。

    OCR/模板是逐框读、漏框即漏检；这里让大模型看整张图数总数，用于核对"是否有该有却没框到的颗粒"。
    异常/缺 key 返回 {}（调用方当"未核对"处理）。
    """
    try:
        obj = _extract_json(_call_qwen([image_path], prompts.COUNT_DRAM))
        return {
            "total": int(obj.get("total", 0) or 0),
            "dist": obj.get("dist", {}) or {},
            "unclear": int(obj.get("unclear", 0) or 0),
            "note": str(obj.get("note", "") or ""),
        }
    except Exception as e:  # noqa: BLE001
        log.info("整图数颗粒失败：%s", e)
        return {}


def appearance_bools(parsed: dict) -> dict:
    """把大模型外观结果归纳成三个 bool + 各自不合格说明（用于入库/前端）。

    返回 {comp_ok, gold_finger_ok, chip_mark_ok, fails:[...]}；fails 为不合格项说明。
    """
    fails = []
    comp = parsed.get("components", {}) or {}
    comp_bad = bool(comp.get("damaged")) or bool(comp.get("blackened"))
    if comp_bad:
        tips = []
        if comp.get("damaged"):
            tips.append("损坏")
        if comp.get("blackened"):
            tips.append("发黑")
        fails.append("元器件：" + "/".join(tips) + (f"（{comp.get('detail')}）" if comp.get("detail") else ""))

    gf = parsed.get("gold_finger", {}) or {}
    gf_ok = gf.get("normal") is not False
    if not gf_ok:
        fails.append("金手指：" + (gf.get("detail") or "异常"))

    cm = parsed.get("chip_marks", {}) or {}
    cm_ok = cm.get("all_have_lines") is not False
    if not cm_ok:
        fails.append("存储芯片二维码标记：出现只有散点、无线条的异常")

    return {"comp_ok": not comp_bad, "gold_finger_ok": gf_ok,
            "chip_mark_ok": cm_ok, "fails": fails}


def _verdict(parsed: dict) -> tuple[bool, list[str]]:
    """根据模型结构化结果做最终判定。返回 (合格?, 红灯原因列表)。"""
    reasons: list[str] = []

    comp = parsed.get("components", {}) or {}
    if comp.get("damaged"):
        reasons.append("电子元器件存在损坏：" + (comp.get("detail") or "见图"))
    if comp.get("blackened"):
        reasons.append("元器件/板面发黑或烧焦：" + (comp.get("detail") or "见图"))

    gf = parsed.get("gold_finger", {}) or {}
    if gf.get("normal") is False:
        reasons.append("金手指异常：" + (gf.get("detail") or "见图"))

    cm = parsed.get("chip_marks", {}) or {}
    if cm.get("all_have_lines") is False:
        reasons.append("存储芯片二维码标记出现只有散点、无线条的异常："
                       + (cm.get("detail") or "见图"))

    # 模型额外列出的问题并入原因（去重）
    for it in parsed.get("issues", []) or []:
        s = str(it).strip()
        if s and s not in reasons:
            reasons.append(s)

    return (len(reasons) == 0), reasons


def inspect_tray(front_path: str | None, back_path: str | None,
                 slots: list, axis: str = "vertical") -> dict | None:
    """**整盘一次**外观质检：一次调用判 n 根，返回 {slot: 单根结果}。

    为什么：逐根调用要发 n×2 张图、付 n 次网络+排队固定开销（实测四根并行仍 ~11.6s）。
    整盘图本来就都拍好了，一次发 2 张让模型逐根输出，能省掉那 n-1 次固定开销。

    严格校验：模型返回的条数必须与 slots 一致，否则视为串位、返回 None 让调用方
    退回逐根调用（宁可慢，不能把某根的结论安到别根上——那会放过坏条）。
    """
    images = [p for p in (front_path, back_path) if p]
    if not images or not slots:
        return None
    try:
        t = time.perf_counter()
        # 整盘原图 2448×2048/1.6MB×2，直接发比逐根裁图(168KB)慢一倍(实测 20.6s vs 11.6s)——
        # 上传量与大图推理才是主导。故整盘调用临时把长边上限压到 TRAY_MAX_SIDE 再发。
        _old = os.environ.get("QWEN_MAX_SIDE")
        os.environ["QWEN_MAX_SIDE"] = os.environ.get("TRAY_MAX_SIDE", "1280")
        try:
            raw = _call_qwen(images, prompts.appearance_tray(len(slots), list(slots), axis))
        finally:
            if _old is None:
                os.environ.pop("QWEN_MAX_SIDE", None)
            else:
                os.environ["QWEN_MAX_SIDE"] = _old
        model_sec = round(time.perf_counter() - t, 2)
        arr = _extract_json_array(raw)
    except Exception as e:  # noqa: BLE001
        log.info("整盘外观质检失败(退回逐根)：%s", e)
        return None
    if not isinstance(arr, list) or len(arr) != len(slots):
        log.info("整盘外观质检条数不符(期望%d 实得%s)，退回逐根", len(slots), len(arr) if isinstance(arr, list) else "非数组")
        return None

    out = {}
    for i, (sl, parsed) in enumerate(zip(slots, arr)):
        if not isinstance(parsed, dict):
            return None
        qualified, reasons = _verdict(parsed)
        ab = appearance_bools(parsed)
        out[sl] = {
            "ok": True,
            "status": "pass" if qualified else "fail",
            "qualified": qualified,
            "reasons": reasons,
            "details": parsed,
            "model": _model(),
            "model_sec": model_sec if i == 0 else 0.0,   # 一次调用的耗时只记一次
            "comp_ok": ab["comp_ok"],
            "gold_finger_ok": ab["gold_finger_ok"],
            "chip_mark_ok": ab["chip_mark_ok"],
            "appearance_fails": ab["fails"],
        }
    return out


def inspect_module(front_path: str | None = None,
                   back_path: str | None = None) -> dict:
    """对正/背面照片做外观质检（至少一张）。

    返回:
      {
        "ok": True/False,                 # 调用是否成功
        "status": "pass"|"fail"|"error",
        "qualified": True/False,          # 是否合格（绿灯）
        "reasons": [...],                 # 不合格原因（红灯时非空）
        "details": {...},                 # 模型结构化结果
        "model": "...",
        "error": "..."                    # 仅出错时
      }
    """
    images = [p for p in (front_path, back_path) if p]
    if not images:
        return {"ok": False, "status": "error", "qualified": None,
                "reasons": [], "details": {}, "model": _model(), "model_sec": 0.0,
                "error": "未提供任何图片"}
    try:
        t = time.perf_counter()
        raw = _call_qwen(images, _PROMPT)        # 仅大模型网络调用计时
        model_sec = round(time.perf_counter() - t, 2)
        parsed = _extract_json(raw)
    except Exception as e:
        return {"ok": False, "status": "error", "qualified": None,
                "reasons": [], "details": {}, "model": _model(), "model_sec": 0.0,
                "error": str(e)}

    qualified, reasons = _verdict(parsed)
    ab = appearance_bools(parsed)
    return {
        "ok": True,
        "status": "pass" if qualified else "fail",
        "qualified": qualified,
        "reasons": reasons,
        "details": parsed,
        "model": _model(),
        "model_sec": model_sec,
        # 三态 bool + 各自不合格说明（入库/前端用）
        "comp_ok": ab["comp_ok"],
        "gold_finger_ok": ab["gold_finger_ok"],
        "chip_mark_ok": ab["chip_mark_ok"],
        "appearance_fails": ab["fails"],
    }
