# -*- coding: utf-8 -*-
"""实测「逐张调 predict」vs「一次送一批」的差距，并核对读出结果是否完全一致。

为什么值得测：
  region_ocr 每个 dram 框要跑 2~3 次 OCR（增强读一次、不够置信再读原图、
  读出后 _tight_digit_box 又整框重跑一次收紧标注框），128 框实际是 250~380 次推理。
  5s / 300 次 ≈ 17ms 一次 —— 单次计算量极小，开销压倒性地在调度
  （CLAUDE.md 记的 GPU 峰值利用率仅 56%，也印证不是算力吃紧）。
  所以该消掉的是"调用次数"，不是"加并发"（加并发是用更多调度去解决调度问题，
  而且 Paddle 官方要求每线程独立 predictor，Python 下每份都要完整加载权重）。

口径：
  用 vl_crop.py 已经裁好的真实颗粒图（不是合成图），保证测的是生产同款输入。
  两轮都跑同一批图、比对**逐图文本**是否一致 —— 只快不准是没有意义的。

用法：
    .venv\\Scripts\\python.exe tools\\batch_probe.py [图数量] [批大小]
"""
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import app.recognition.ocr_engine as oe  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 20
BS = int(sys.argv[2]) if len(sys.argv) > 2 else 0      # 0 = 一次全送

CROPS = sorted(glob.glob("logs/_vl/crops/*/s*_dram*.png"))[:N]
if not CROPS:
    sys.exit("没找到裁好的颗粒图，先跑 vl_crop.py")

oe.configure(device=os.environ.get("OCR_DEVICE", "gpu"),
             use_server_models=os.environ.get("OCR_SERVER_MODELS", "1") == "1",
             det_limit_side_len=int(os.environ.get("OCR_DET_SIDE_LEN", "1536")),
             tile_bands=1)

arrs = [np.asarray(Image.open(p).convert("RGB")) for p in CROPS]
print(f"图 {len(arrs)} 张  尺寸 {arrs[0].shape}  设备 {oe._effective_device()}")

engine = oe.get_engine()
# 预热：首次调用含 kernel 编译/显存分配，不计入
oe._predict_array(engine, arrs[0])
print("预热完成\n")


def texts_of(dets):
    return tuple(sorted(d["text"].strip() for d in dets if d.get("text", "").strip()))


# ---------------------------------------------------------------- 逐张
t0 = time.perf_counter()
one_by_one = [texts_of(oe._predict_array(engine, a)) for a in arrs]
t_serial = time.perf_counter() - t0
print(f"逐张  {t_serial:6.2f}s   单张均 {t_serial/len(arrs)*1000:5.0f}ms")

# ---------------------------------------------------------------- 成批
# PaddleOCR 3.x 的 predict(input=) 收 list：一次调用内部走批，
# 省掉 N-1 次的预处理/显存搬运/kernel 启动开销。
batches = [arrs] if BS <= 0 else [arrs[i:i + BS] for i in range(0, len(arrs), BS)]
t0 = time.perf_counter()
batched = []
try:
    for grp in batches:
        for res in engine.predict(input=grp):
            batched.append(texts_of(oe._extract(res)))
    t_batch = time.perf_counter() - t0
except Exception as e:                               # noqa: BLE001
    print(f"批量调用失败: {type(e).__name__}: {e}")
    sys.exit(1)

print(f"成批  {t_batch:6.2f}s   单张均 {t_batch/len(arrs)*1000:5.0f}ms   "
      f"批大小 {'全部' if BS <= 0 else BS}")
print(f"\n加速比 {t_serial/t_batch:.2f}×")

# ---------------------------------------------------------------- 一致性
print("\n" + "=" * 56)
if len(batched) != len(one_by_one):
    print(f"⚠ 返回张数不一致：逐张 {len(one_by_one)} / 成批 {len(batched)}")
    print("  批量若不保序或合并了结果，就不能直接替换 —— 会串位。")
else:
    diff = [(i, a, b) for i, (a, b) in enumerate(zip(one_by_one, batched)) if a != b]
    if not diff:
        print(f"✓ {len(arrs)} 张逐图文本完全一致，批量可安全替换")
    else:
        print(f"⚠ {len(diff)} 张结果不同（批量不能盲目替换）：")
        for i, a, b in diff[:8]:
            print(f"  [{i}] {os.path.basename(CROPS[i])}")
            print(f"      逐张 {a}")
            print(f"      成批 {b}")
