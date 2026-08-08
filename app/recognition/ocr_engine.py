"""PaddleOCR 引擎封装（PP-OCRv5）。

- 懒加载单例，首次调用时初始化（首次会自动下载模型）。
- 兼容 PaddleOCR 3.x 的 predict() 返回结构，并归一化为统一 detection 列表。
- 支持 GPU / CPU 切换。
"""
from __future__ import annotations

import os
import threading
from typing import Optional

import numpy as np

_ocr = None
_lock = threading.Lock()
# 默认服务端高精度模型 + 高分辨率检测 + 分块识别：
# 内存条满板密集小字(每颗 DRAM 一个"SEC 534")，整图一次识别会漏检；
# 切成若干横条分别识别再合并去重，召回大幅提升（实测正反两面 27→35 颗粒全中）。
_config = {
    "device": "gpu",
    "lang": "en",
    "use_server_models": True,
    "det_limit_side_len": 2048,
    "tile_bands": 3,        # 横向分块条数；1=关闭分块（仅整图 recognize() 用，模板模式不走）
    "tile_overlap": 0.18,   # 相邻条重叠比例，避免切断芯片
    # 文本行方向分类：默认关（见 _build_ocr 注释）。竖排标签等确需时可 configure(...=True)
    "use_textline_orientation": False,
}


def configure(device: str = "gpu", lang: str = "en",
              use_server_models: bool = True, det_limit_side_len: int = 2048,
              tile_bands: int = 3, tile_overlap: float = 0.18,
              use_textline_orientation: bool = False):
    """在首次初始化前调用以覆盖默认配置。"""
    _config.update(device=device, lang=lang,
                   use_server_models=use_server_models,
                   det_limit_side_len=det_limit_side_len,
                   tile_bands=tile_bands, tile_overlap=tile_overlap,
                   use_textline_orientation=use_textline_orientation)


# 模型放在项目内 models/（随项目走，换机/离线部署不必再从网上下载）。
# 目录不存在时回退到按模型名走 PaddleOCR 默认缓存(~/.paddlex)，保证老环境照旧能跑。
_MODELS_DIR = os.environ.get(
    "OCR_MODELS_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "models"),
)


def _model_dir(name: str) -> Optional[str]:
    """项目内模型目录（存在且含 inference 文件才用），否则 None → 走默认缓存。"""
    d = os.path.join(_MODELS_DIR, name)
    if os.path.isdir(d) and any(f.startswith("inference") for f in os.listdir(d)):
        return d
    return None


def _effective_device() -> str:
    """实际生效的设备：配了 gpu 但机器无可用 CUDA 卡时，Paddle 会**静默回落 CPU**
    （只打一行 UserWarning），_config 里却仍写着 "gpu"。后端选择依赖真实设备，
    故这里探测一次，返回真正会用的设备名。"""
    want = str(_config.get("device", "gpu")).lower()
    if not want.startswith("gpu"):
        return want
    try:
        import paddle
        if paddle.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0:
            return want
    except Exception:  # noqa: BLE001
        pass
    return "cpu"


def _build_ocr():
    from paddleocr import PaddleOCR

    device = _effective_device()
    kwargs = dict(
        lang=_config["lang"],
        use_doc_orientation_classify=False,  # 整图方向分类，单芯片照片用不上
        use_doc_unwarping=False,             # 文档摆正，用不上
        # 文本行方向分类器：关掉。颗粒/主控是贴片，方向由 SMT 固定；唯一倒印的 PCB 丝印
        # 已在 region_ocr._best_read(try_rot180=True) 里显式试 180°，不需要每框再跑一个
        # 分类模型。实测关掉后正面 3.06→2.13s、反面 5.16→3.40s，读出率不变(44/44、84/84)。
        use_textline_orientation=_config.get("use_textline_orientation", False),
        device=device,                       # 真实可用设备（无 N 卡时自动为 cpu，见 _effective_device）
        text_det_limit_side_len=_config["det_limit_side_len"],  # 高分辨率，保住小字
        text_det_limit_type="max",
        # 放宽检测阈值，提高低对比度小字召回
        text_det_box_thresh=0.3,
        text_det_thresh=0.2,
        text_det_unclip_ratio=2.0,
    )
    # CPU 推理后端：PaddleOCR 在 CPU 上默认开 mkldnn(oneDNN)，但 paddle 3.3.1 的 oneDNN
    # 新执行器缺 ConvertPirAttribute2RuntimeAttribute 对 ArrayAttribute<Double> 的实现，
    # 跑 PP-OCRv5_server_det 直接抛 NotImplementedError（无 N 卡、回落 CPU 时必然踩到）。
    # 关掉 mkldnn 后 PaddleX 会走原生 paddle 后端（慢些但能跑）。OCR_MKLDNN=1 可强开。
    if device.startswith("cpu"):
        kwargs["enable_mkldnn"] = os.environ.get("OCR_MKLDNN", "0") == "1"
        threads = int(os.environ.get("OCR_CPU_THREADS", "0") or 0)
        if threads > 0:
            kwargs["cpu_threads"] = threads
    if _config["use_server_models"]:
        # 服务端模型精度更高，适合小字/低对比度芯片丝印
        det_name, rec_name = "PP-OCRv5_server_det", "PP-OCRv5_server_rec"
        kwargs.update(
            text_detection_model_name=det_name,
            text_recognition_model_name=rec_name,
        )
        det_dir, rec_dir = _model_dir(det_name), _model_dir(rec_name)
        if det_dir and rec_dir:                  # 用项目内模型（离线可用，不依赖用户目录缓存）
            kwargs.update(text_detection_model_dir=det_dir,
                          text_recognition_model_dir=rec_dir)
    return PaddleOCR(**kwargs)


def get_engine():
    global _ocr
    if _ocr is None:
        with _lock:
            if _ocr is None:
                _ocr = _build_ocr()
    return _ocr


def _poly_to_box(poly) -> list:
    """把各种多边形表示归一化为 [[x,y]*4] 的 python list。"""
    arr = np.asarray(poly).reshape(-1, 2)
    return [[float(x), float(y)] for x, y in arr]


def _extract(res) -> list[dict]:
    """从单张图的 PaddleOCR 3.x 结果对象抽取 detection 列表。"""
    # 3.x 结果对象类似 dict，常见键如下
    def get(key):
        try:
            return res[key]
        except Exception:
            return getattr(res, key, None)

    texts = get("rec_texts")
    scores = get("rec_scores")
    polys = get("rec_polys")
    if polys is None:
        polys = get("dt_polys")
    if polys is None:
        polys = get("rec_boxes")

    detections = []
    if texts is not None and polys is not None:
        for i, text in enumerate(texts):
            score = float(scores[i]) if scores is not None and i < len(scores) else 0.0
            try:
                box = _poly_to_box(polys[i])
            except Exception:
                box = None
            detections.append({"text": str(text), "score": score, "box": box})
    return detections


def _aabb(box):
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return min(xs), min(ys), max(xs), max(ys)


def _iou(a, b) -> float:
    ax0, ay0, ax1, ay1 = _aabb(a)
    bx0, by0, bx1, by1 = _aabb(b)
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = (ax1 - ax0) * (ay1 - ay0)
    area_b = (bx1 - bx0) * (by1 - by0)
    return inter / (area_a + area_b - inter + 1e-6)


def _nms(detections: list[dict], iou_thresh: float = 0.35) -> list[dict]:
    """跨分块的重复检测去重：框高度重叠时保留分数最高者。"""
    kept: list[dict] = []
    for det in sorted(detections, key=lambda d: -d.get("score", 0)):
        if det.get("box") is None:
            kept.append(det)
            continue
        dup = False
        for k in kept:
            if k.get("box") is None:
                continue
            # 同位置且文本相近（或其一是另一的子串）视为重复
            if _iou(det["box"], k["box"]) >= iou_thresh:
                a, b = det["text"].replace(" ", ""), k["text"].replace(" ", "")
                if a == b or a in b or b in a or _iou(det["box"], k["box"]) >= 0.6:
                    dup = True
                    break
        if not dup:
            kept.append(det)
    return kept


def _predict_array(engine, arr):
    out = []
    for res in engine.predict(input=arr):
        out.extend(_extract(res))
    return out


def recognize(image_path: str) -> list[dict]:
    """对单张图片做 OCR，返回 [{text, score, box}]。

    流程：整图识别 + 横向分块识别 → 合并 → NMS 去重。
    分块显著提升满板密集小字的召回。
    """
    import numpy as np
    from PIL import Image

    engine = get_engine()
    img = Image.open(image_path).convert("RGB")
    W, H = img.size
    full = np.asarray(img)

    detections: list[dict] = _predict_array(engine, full)

    bands = int(_config.get("tile_bands", 1))
    overlap = float(_config.get("tile_overlap", 0.18))
    if bands >= 2:
        bh = int(H / (bands - (bands - 1) * overlap))
        step = max(1, int(bh * (1 - overlap)))
        y = 0
        while y < H:
            y2 = min(H, y + bh)
            crop = np.asarray(img.crop((0, y, W, y2)))
            for d in _predict_array(engine, crop):
                if d.get("box"):
                    d["box"] = [[x, yy + y] for x, yy in d["box"]]  # 坐标映射回原图
                detections.append(d)
            if y2 >= H:
                break
            y += step

    return _nms(detections)
