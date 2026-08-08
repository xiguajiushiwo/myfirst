# -*- coding: utf-8 -*-
"""把 vl_crop.py 裁好的块逐块交多模态大模型读日期，统计**总耗时**与**token 消耗**。

为什么逐块（每块一次调用）而不是拼多图一次调用：
  业务铁律是"逐颗看清每一颗的日期"，禁止多数表决 —— 拼多图一次调用存在
  **返回顺序串位**风险，会把某颗的日期安到另一颗身上，等于放过被偷换的那颗。
  逐块调用每张只对应一颗，串位在结构上不可能发生。
  代价是调用次数多，所以用线程池并发把墙钟压下来。

统计口径：
  耗时  = 整批的墙钟（并发后的真实等待时间），另给单次调用的均值/中位/最大。
  token = app.metrics 在整批前后的 vl_usage 差值（prompt / completion / total），
          即真实计费口径，不是估算。

用法：
    .venv\\Scripts\\python.exe tools\\vl_read.py logs\\_vl\\crops\\<stem>\\index.json [并发数]
产物：
    logs/_vl/crops/<stem>/vl_result.json
"""
import json
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from PIL import Image  # noqa: E402

from app import metrics, prompts  # noqa: E402
from app.inspection import quality_inspect as qi  # noqa: E402

IDX = sys.argv[1] if len(sys.argv) > 1 else None
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 8
if not IDX:
    sys.exit("用法: vl_read.py logs/_vl/crops/<stem>/index.json [并发数]")

ROOT = os.path.dirname(IDX)
with open(IDX, encoding="utf-8") as f:
    index = json.load(f)

# PCB 一盘之内朝向不统一 → vl_crop 存了 r0/r180 两张。
# 这里只送 r0，让大模型自己认倒字（它不像 OCR 的 det 那样依赖行方向）；
# r0 读不出时再送 r180 兜底，避免无谓地把调用次数翻倍。
blocks = [b for b in index["blocks"] if not b["file"].endswith("_r180.png")]
fallback = {b["slot"]: b for b in index["blocks"] if b["file"].endswith("_r180.png")}

print(f"图 {index['src']}")
print(f"块 {len(blocks)} 张（PCB 先只送正向，读不出再送倒向）  并发 {WORKERS}")
print(f"模型 {qi._model()}\n")


# 每类一套提示词。**关键**：三类丝印都有"不是日期的数字行"（颗粒底部料号、
# PMIC 第四行 18-61、SOT 的 8Y1/5KR），上一轮用同一套笼统提示词的结果是
# 颗粒把料号 QRY40623C 读成 406、PMIC 四槽全返回 1861 —— 静默错读，
# 比读不出危险得多。所以按类给出行位置约束，见 app/prompts.py 的注释。
PROMPT = {
    "dram": prompts.crop(prompts.want_text("dram")),
    "pmic": prompts.PMIC_DATE,
    "sot":  prompts.SOT_DATE,
    "pcb":  prompts.PCB_DATE,
}


def ask(b: dict) -> dict:
    """读单块。返回 {**b, digits, raw, sec}。"""
    # SOT 与 dram 都是 3 位（YWW），pmic/pcb 是 4 位（YYWW）。
    # 上一版把 SOT 也按 4 位校验，结果槽1 明明读对了 511 却被判"位数不符"作废。
    want = 3 if b["kind"] in ("dram", "sot") else 4
    t0 = time.perf_counter()
    digits, raw, err = "", "", ""
    try:
        im = Image.open(os.path.join(ROOT, b["file"]))
        content = [{"type": "text", "text": PROMPT[b["kind"]]},
                   {"type": "image_url", "image_url": {"url": qi._img_data_url(im)}}]
        obj = qi._extract_json(qi._chat(content))
        d = re.sub(r"\D", "", str(obj.get("digits", "")))
        raw = str(obj.get("raw", ""))[:60]
        # 位数不符一律作废：宁可留空转人工，不接受一个位数都不对的数字
        digits = d if len(d) == want else ""
        if d and not digits:
            err = f"位数不符({d})"
        # 周数合法性校验（1~53）。是硬约束，能拦住"取错了行"这类静默错读 ——
        # 实测 PCB 槽2 从 '2534 09-03' 取了 09-03 → 0903，后两位 03 虽合法
        # 但年份 09 不合理，所以年份也一并卡（本项目在 2020 年代）。
        if digits:
            wk = int(digits[-2:])
            yr = int(digits[:-2]) if want == 4 else int(digits[0])
            bad = not (1 <= wk <= 53) or (want == 4 and not (20 <= yr <= 30))
            if bad:
                err = f"日期不合法({digits}) raw={raw!r}"
                digits = ""
    except Exception as e:                       # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    return {**b, "digits": digits, "raw": raw, "err": err,
            "sec": round(time.perf_counter() - t0, 2)}


before = metrics.vl_usage()
t_all = time.perf_counter()
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    results = list(ex.map(ask, blocks))

# PCB 正向读不出的，补一次倒向
retry = [fallback[r["slot"]] for r in results
         if r["kind"] == "pcb" and not r["digits"] and r["slot"] in fallback]
if retry:
    print(f"PCB 正向未读出 {len(retry)} 个，补送倒向\n")
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r2 in ex.map(ask, retry):
            for i, r in enumerate(results):
                if r["kind"] == "pcb" and r["slot"] == r2["slot"] and not r["digits"]:
                    results[i] = r2
                    break
wall = time.perf_counter() - t_all
usage = metrics.vl_usage_delta(before)

# ---------------------------------------------------------------- 结果
by_slot = {}
for r in results:
    by_slot.setdefault(r["slot"], []).append(r)

for si in sorted(by_slot):
    rs = by_slot[si]
    drams = sorted([r for r in rs if r["kind"] == "dram"], key=lambda r: r["idx"])
    got = [r["digits"] for r in drams if r["digits"]]
    uniq = sorted(set(got))
    print(f"=== 槽{si} ===")
    print(f"  颗粒 {len(drams)} 颗，读出 {len(got)} 颗，不同日期 {uniq}")
    blind = [r["idx"] for r in drams if not r["digits"]]
    if blind:
        print(f"  ⚠ 盲点（未读出，须转人工）：颗粒 {blind}")
    # 逐颗列出，任何一颗与其余不同都要能定位到具体颗号（禁止多数表决）
    print("  逐颗: " + "  ".join(f"{r['idx']:02d}:{r['digits'] or '??'}" for r in drams))
    for k in ("pmic", "sot", "pcb"):
        for r in rs:
            if r["kind"] == k:
                # 连 raw 一起打：raw 是模型自称"看到的原文"，
                # 一旦 digits 与 raw 不符就能立刻发现是取错了行，而不是等到判定阶段
                print(f"  {k:5s} {r['digits'] or '?? 未读出'}  ({r['sec']}s"
                      f"{' ' + r['err'] if r['err'] else ''})  raw={r.get('raw', '')!r}")
    print()

secs = [r["sec"] for r in results]
n_ok = sum(1 for r in results if r["digits"])
print("=" * 56)
print(f"块数 {len(results)}  读出 {n_ok}  未读出 {len(results) - n_ok}")
print(f"总墙钟 {wall:.1f}s （并发 {WORKERS}）")
print(f"单块耗时 均值 {statistics.mean(secs):.1f}s  中位 {statistics.median(secs):.1f}s  "
      f"最大 {max(secs):.1f}s  串行累计 {sum(secs):.0f}s")
print(f"调用次数 {usage['calls']}")
print(f"token  prompt {usage['prompt_tokens']}  completion {usage['completion_tokens']}  "
      f"total {usage['total_tokens']}")
if usage["calls"]:
    print(f"       平均每块 {usage['total_tokens'] / usage['calls']:.0f} token")

out = {"src": index["src"], "model": qi._model(), "workers": WORKERS,
       "wall_sec": round(wall, 2), "usage": usage,
       "per_call_sec": {"mean": round(statistics.mean(secs), 2),
                        "median": round(statistics.median(secs), 2),
                        "max": round(max(secs), 2), "sum": round(sum(secs), 1)},
       "results": results}
with open(f"{ROOT}/vl_result.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(f"\n明细 → {ROOT}/vl_result.json")
