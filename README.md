# 云小圈AI硬件质检系统

对内存条照片做两件事：

1. **日期码识别**（PaddleOCR PP-OCRv5 server + GPU）：识别 PCB / 存储颗粒 / 主控 三类日期码，解码成「年 / 周」，画出清晰标注图，并按所有日期的最大周差给出**合格 / 不合格信号灯**。两种识别方式可切换：
   - **规则识别（默认）**：整图 OCR 后**按规则在全图自动找出**所有日期码（颗粒 SEC###、主控 YYWW 序列号前缀、PCB 4 位），**不依赖固定坐标框、不需要模板**，适配任意型号。
   - **模板识别**：正/背面按**所选型号模板**的固定框坐标套用，只识别颗粒。
   - **PCB 槽**（两种方式下都一样）：丝印对比度低，**同时用 PaddleOCR 与 Qwen-VL 大模型识别再综合**（一致采用 / 只有一方采用那个 / 不一致取大模型并标冲突）。
   两种方式都输出**原生识别结果**：能解码就给日期（不做多数校正/预测，误读也照实给），不能解码就把 OCR 原始读数原样写上。
2. **外观质检**（通义千问 Qwen-VL 多模态大模型）：检查元器件是否损坏/发黑、金手指是否正常、每颗存储芯片的二维码标记是否有线条；任一异常即判不合格并给出原因。

**综合判定（单一信号灯）**：页面顶部只有**一个**合格/不合格灯——**只有「日期」与「外观」两项都合格才亮绿灯**，否则红灯并把两边的问题逐条列出。日期合格判据**只有一个指标**：所有日期的最大周差 ≤ 阈值（阈值前端可调），> 阈值即不合格。**逐颗比较、严禁多数表决**：必须看清每一颗日期，任何一颗与其余不一致即判不合格并定位到第几颗——因为要能查出「有人偷换单颗存储芯片」（多数表决会把被换的芯片当误读放过，属最严重漏判）。读不出的颗粒是盲点，单列出来提示人工确认（原始透明输出，不显示"遮挡"）。

**型号模板库**：不同品牌/型号的内存条排布不同，各自一套固定框坐标，存于 `app/templates/`，识别时手动下拉选择。日期识别与外观质检的耗时分开计时、前端分别展示。

**质检记录持久化（MySQL）**：每次质检可**绑定序列号(SN) + 操作人**存入 MySQL（`app/storage/db.py`），页面「质检记录」卡可查询追溯。操作人在页面右上角可**选择 / 新增 / 删除**。前端风格与官网 [yunxiaoquan.cn](https://www.yunxiaoquan.cn/) 统一（浅色蓝主题）。

技术栈：Python 3.12 · FastAPI · PaddleOCR 3.7 (paddlepaddle-gpu, CUDA 12.6) · 通义千问 Qwen3-VL（DashScope 接口）。

---

## 一、目录与文件总览

```
AIpaddle/
├─ app/                    后端 Python 包（按职责分子包）
│  ├─ __init__.py          标记 app 为 Python 包
│  ├─ server.py            FastAPI **入口**（`uvicorn app.server:app`）：建 app、挂静态、建库、注册各 router；只留 首页/health（~90 行）
│  ├─ core.py              共享配置：路径常量、槽位定义(SLOT_LABELS/KIND)、判定阈值、图片扩展名
│  ├─ services.py          识别/质检**服务层**：run_recognize / analyze_all(三路并行) / compute_signal(逐颗判定) / build_record / analyze_and_save / resolve_set / save_upload
│  ├─ routers/             【接口分组】按职责拆
│  │  ├─ recognition.py    识别/模板/文件夹/质检：/api/recognize · /inspect · /templates · /read_label · /folder/*
│  │  ├─ cameras.py        相机：/api/camera/* (UVC) · /api/hik/* (海康：预览/抓拍/曝光)
│  │  ├─ records.py        记录/操作人：/api/records/* · /api/operators/*
│  │  └─ pipeline.py       自动流水线：/api/pipeline/start·stop·status·stream(SSE)
│  ├─ recognition/         【日期识别】子包
│  │  ├─ ocr_engine.py     PaddleOCR 引擎封装（懒加载、整图+分块识别、NMS 去重）
│  │  ├─ date_parser.py    日期码解码/分类/汇总（纯逻辑，不依赖 paddle）
│  │  ├─ region_ocr.py     模板固定框识别（多方案 OCR+方向翻正+增强+字符挽救）+ 规则识别 + PCB/主控特写
│  │  ├─ visualize.py      在图上画框、中文标签排两侧清晰标注（解码=类型色，原始读数=橙；不显示遮挡）
│  │  ├─ template_store.py 型号模板库 CRUD（扫描 templates/ 目录，按品牌/型号管理坐标）
│  │  └─ templates/        型号模板目录，每型号一个 <id>.json（丢 JSON 进来即生效）
│  │     └─ samsung-m321r8ga0pb0.json   首个/默认模板（按 demo3 手工标注每颗日期位置）
│  ├─ inspection/          【外观质检 / 大模型】子包
│  │  └─ quality_inspect.py 外观质检 + 读标签/PCB/颗粒（Qwen-VL），判合格并给红灯原因
│  ├─ cameras/             【相机采集】子包
│  │  ├─ camera.py         服务器端双相机采集（OpenCV/UVC，正背面两台，实时预览+拍照）
│  │  └─ hik_camera.py     海康工业相机（MVS SDK，双相机 上/下 按SN绑定，同时抓正反 capture_both）
│  ├─ pipeline/            【自动流水线】子包
│  │  ├─ feeder.py         来料源：相机输出目录监听(FolderWatchFeeder)——托盘拍完写文件夹即自动识别（弹仓/电机/串口握手已弃用）
│  │  └─ runner.py         自动识别循环：来料就绪→处理→标记完成→下一份，后台循环 + SSE（原 pipeline.py）
│  └─ storage/             【持久化】子包
│     └─ db.py             MySQL：质检记录（绑定 SN+操作人）+ 操作人 CRUD
├─ web/
│  ├─ app.css              共享设计系统（对齐「云小圈交易同步系统」看板风格：顶栏+我的菜单、筛选栏、卡片看板、标签）
│  ├─ index.html           质检工作台（新顶栏+菜单；当前批次 + 综合判定 + 读取文件 + 实时画面 + 识别结果）
│  ├─ station.html         质检工位屏 `/station`（全屏零点击；设备喂料自动识别，大号合格/不合格灯 + 本批良率）
│  ├─ multi.html           一图多根识别 `/multi`（实验：一张 N 根等分切、逐根识别；新增接口不改原逻辑）
│  ├─ manage.html          质检管理看板 `/manage`（KPI + 筛选工具栏 + 批次/质检记录 双看板卡片）
│  └─ settings.html        系统设置 `/settings`（多模态大模型 多provider 配置 + 用量/费用）
├─ run.bat                 Windows 启动脚本（设 UTF-8 + 启 uvicorn）
├─ pyproject.toml          项目元数据（uv 管理，requires-python>=3.12）
├─ .env                    密钥配置（DASHSCOPE_API_KEY、QWEN_VL_MODEL；已 gitignore）
├─ .python-version         指定 Python 版本（3.12）
├─ .gitignore             Git 忽略项（含 .env 与运行期产物）
├─ uploads/                双相机采集原图：每次抓拍存 `uploads/<序号>/front.jpg` + `back.jpg`（序号 0001、0002… 递增，代表拍摄先后；根目录只含有序子文件夹，不堆散图）
├─ outputs/                生成的标注图（运行期写入、已忽略，前端按 URL 取用）
├─ samples/                参考样例标注图（region_front/back.png，当前管线效果）
└─ .venv/                  uv 创建的虚拟环境（Python 3.12 + paddle-gpu）
```

---

## 二、各文件功能 + 函数方法详解

### app/recognition/ocr_engine.py — PaddleOCR 引擎封装

懒加载单例，把 PaddleOCR 3.x 的返回归一化成统一的 `{text, score, box}` 列表；对满板密集小字用「整图 + 横向分块」两遍识别再去重，提升召回。

模块级配置 `_config`：device、lang、use_server_models、det_limit_side_len、tile_bands（分块条数）、tile_overlap（条间重叠比）。

| 函数 | 作用 |
|---|---|
| `configure(device, lang, use_server_models, det_limit_side_len, tile_bands, tile_overlap)` | 在首次初始化前覆盖默认配置（被 server.py 按环境变量调用） |
| `_build_ocr()` | 按 `_config` 构造 `PaddleOCR` 实例；开服务端高精度模型时指定 `PP-OCRv5_server_det/rec`，并放宽检测阈值以召回低对比度小字 |
| `get_engine()` | 线程安全的懒加载单例，首次调用才真正初始化（含模型下载） |
| `_poly_to_box(poly)` | 把任意形状的多边形表示统一成 `[[x,y]*4]` 的 Python list |
| `_extract(res)` | 从单张图的 PaddleOCR 3.x 结果对象里抽出 `[{text, score, box}]`（兼容 rec_texts/rec_polys/dt_polys 等键） |
| `_aabb(box)` | 求多边形的轴对齐外接框 (minx,miny,maxx,maxy) |
| `_iou(a, b)` | 两个框的交并比 IoU |
| `_nms(detections, iou_thresh=0.35)` | 跨分块重复检测去重：框高度重叠且文本相同/包含时保留分数最高者 |
| `_predict_array(engine, arr)` | 对一个 numpy 图像数组跑 `engine.predict`，返回归一化 detection 列表 |
| `recognize(image_path)` | **对外主函数**：整图识别 + 横向分块识别 → 合并 → NMS 去重，返回 `[{text,score,box}]` |

### app/recognition/date_parser.py — 日期码解码 / 分类 / 汇总

纯逻辑、不依赖 paddle，可单测。把 OCR 文本里的日期码识别出来并分三类解码，用「严格 token 形态 + 周数合法性 + 邻近厂商关键字」三重约束抑制料号/规格的误判。

模块级数据：`CONTROLLER_KEYWORDS`（主控厂商关键字）、`DRAM_KEYWORDS`（颗粒厂商关键字）、`TYPE_LABELS`（类型中文名）、`_VENDOR_ANCHORS`（SEC/SAMSUNG 锚点）；正则 `_RE_SERIAL_PREFIX`（4 位+字母序列号前缀）、`_RE_PURE4`（纯 4 位）、`_RE_PURE3`（纯 3 位）、`_RE_SEC_DATE`（SEC/SAMSUNG + 2~3 位）。

**`DateCode`（dataclass）** — 一个识别出的日期码候选：字段 `raw, code_type, year, week, week_start, confidence, source_text, box, digit_format, note, status`。
- `status`：`ok`（已解码出年/周）/ `raw`（未能解码，`raw` 字段为 OCR 原始读数，原样展示）/ `unknown`（特写芯片完全没读到，仅 recognize_chip 用）。
- 属性 `type_label` → 类型中文名；属性 `description` → 「YYYY 年第 N 周」；
- 方法 `to_dict()` → 转 dict（附带 type_label、description、status），供 JSON 返回。

| 函数 | 作用 |
|---|---|
| `_week_start_date(year, week)` | 返回该 ISO 周周一的日期字符串（`fromisocalendar`），非法返回空串 |
| `_valid_week(week)` | 周数是否在 1~53 |
| `_decode_yyww(digits, current_year)` | 解码 4 位 YYWW（00-69→20xx，70-99→19xx；不超当前年+1），返回 (year, week) |
| `_decode_yww(digits, current_year)` | 解码 3 位 YWW（首位=年份个位，按十年就近推断年份） |
| `_center(box)` / `_dist(a, b)` | 框中心点 / 两点欧氏距离（空间邻近判断用） |
| `_context_string(idx, detections, radius)` | 收集自身 + 空间邻近文本拼成小写上下文串，供关键字匹配 |
| `_has_keyword(ctx, keywords)` | 上下文里是否含某类关键字 |
| `_has_adjacent_vendor(idx, detections, radius)` | 裸 3 位码是否紧邻 SEC/SAMSUNG 锚点（同一行很近），过滤碎片误判 |
| `parse_detections(detections, current_year=None, correct=True)` | **核心规则引擎**：遍历 OCR 结果，依次按「主控序列号前缀 / SEC 日期行 / 纯 4 位 / 紧邻锚点的纯 3 位」归类解码。`correct=True` 旧行为（多数补全/降权）；`correct=False` **原生模式**——读残行原样作为 `status="raw"` 输出、不预测。按类型+置信度排序返回 `[DateCode]`。**规则识别模式用 correct=False** |
| `summarize(codes)` | 按 (类型,年,周) 去重聚合，统计次数与最高置信度，供前端概览 |

> 注：现版本正/背面走 region_ocr 的模板固定框流程；`parse_detections` 现主要供「用 PaddleOCR 自动框生成模板」的（用户将来编写的）脚本使用。

### app/recognition/template_store.py — 型号模板库

按品牌+型号管理固定框坐标。每个模板 = `app/templates/<id>.json`，含 `id/brand/model/note/created` 与 `sides.{front,back}.{image_size,boxes}`。**不维护索引文件**：列表直接扫描目录——将来用 PaddleOCR 批量自动框生成的 JSON 丢进目录即自动生效。

| 函数 | 作用 |
|---|---|
| `_slugify(text)` | 品牌+型号 → 文件名安全的 id |
| `_counts(tpl)` | 统计模板各面/各类型框数，供前端列表 |
| `list_templates()` | 扫描目录返回各模板元信息（不含完整 boxes），排序 |
| `get_template(id)` | 取完整模板（带缓存） |
| `default_template_id()` | 识别时未指定模板的兜底（列表第一个） |
| `delete_template(id)` | 删除模板文件 |
| `save_template(brand, model, note, sides, id=None, created="")` | 写入/覆盖模板，返回元信息。**供将来「自动框生成模板」脚本调用**，本轮前端不用 |

### app/recognition/region_ocr.py — 模板固定框识别 + 特写识别

正/背面**只识别存储颗粒**（按所选型号模板的固定框逐处裁剪识别），输出**原生结果，不做任何多数校正/预测**；PCB/主控为单独特写照片整图识别（不走模板）。

| 函数 | 作用 |
|---|---|
| `_denorm(box, W, H)` | 模板归一化坐标 → 当前图像素坐标 |
| `_bbox(box)` | 框的外接矩形 (x0,y0,x1,y1) |
| `_ocr_crop(engine, img, box_px, pad_ratio=0.6, min_h=96)` | 裁框周边局部图（带余量），过小则放大保住小字，OCR 后返回文本列表 |
| `recognize_rules(image_path, current_year=None)` | **规则识别主函数**：整图 `recognize`（含分块）→ `parse_detections(correct=False)` 在全图按规则自动找出所有日期码（颗粒/主控/PCB），不依赖固定坐标框、不需模板、不预测。返回 `[DateCode]` |
| `_read_dram(texts, year)` | **模板模式原生识别**：取 OCR 读数（SEC 后数字 / 独立 3 位 / 2~4 位 / 数字最多的 token），干净 3 位才解码为 YWW（原样，不校正，误读 531 也给 25年31周）；否则返回原始读数串。返回 (year, week, raw) |
| `recognize_side(image_path, side, current_year=None, template_id=None, occ_out=None)` | **正/背面主函数**：从 `template_store` 取模板（缺省 default）逐框识别。**托盘空位**：识别前先 `detect_occupied_slots` 判每个槽有没有条，**空槽整槽跳过**（不识别/不兜底/不判定），占用概要写入 `occ_out`；每颗结果带 `slot` 序号。每颗走 `_best_read` 多方案 OCR；**置信 < `OCR_CONF_MIN` 或未解码 → 逐颗大模型兜底**(`_vl_fallback_dram`)。**原始透明输出**：保留每颗真实 OCR 读数/置信，读错也照显，读不出就显示 OCR 原文——**不再标「遮挡」**。**无多数校正、无填充、无预测。** 返回 `[DateCode]` |
| `detect_occupied_slots(img, slot_rects, thr=None)` | **托盘槽位占位检测（先数数量）**：每个槽算**边缘像素占比**(`cv2.Canny`)——空槽=光滑塑料→接近 0，有条=密集芯片+激光小字→高；`≥ SLOT_PRESENCE_MIN`(默认0.045，可环境变量覆盖) 判有条。检测异常保守判"有条"（宁多勿漏）。返回 `[{slot,occupied,score,box}]`（左→右） |
| `_box_kind(img, box_px)` | **标签/白区框位判别**：框区**又白又平**(平均亮度≥`LABEL_WHITE_MIN` 且边缘占比≤`LABEL_EDGE_MAX`)→`label`，否则 `chip`。`recognize_side` 里判为 `label` 的框**整框跳过**——被白标签盖住的芯片不识别，**防固定框把标签上的 SN 数字误读成日期**（被盖芯片"不用管"）。异常保守判 `chip` |
| `_slot_rects_for_layout(layout)` / `_auto_slot_rects(boxes)` | 取该面槽位矩形：优先模板显式 `slots:[[x0,y0,x1,y1],…]`（真机 4 槽模板务必写死）；无则按框 x 跨度并集自动聚类兜底（双列/紧排易误切）。`_slot_of_box` 按框中心归属槽 |
| `_best_read(engine, crop, year)` | **多方案提准**：①原样读，达标即返回省时；②翻转 180° 判方向（下半区芯片倒装，取文字置信更高的朝向为"正"）；③对正确朝向做 CLAHE 对比增强重读。选择优先级：能解码 > 置信高。返回选中读数 + `best_img` |
| `_enhance(crop)` | 局部对比增强(CLAHE)，救回低对比/发灰的激光打标小字 |
| `_prep_for_vl(engine, crop)` | **发大模型前的统一预处理**：判方向翻正 + 对比增强。是发给 VL 的唯一入口所调用，保证任何路径（模板/规则、上传/文件夹/流水线）发过去的都是方向正确、对比清晰的图 |
| `_read_dram(dets, year)` | 从 OCR 读数里取日期：SEC/SAMSUNG 后 3 位 → 独立 3 位 → 任意 2~4 位。含**字符混淆挽救**(`_RE_SEC_LOOSE`+`_CONF_MAP`)：SEC 后形近字母映射回数字（`SECS40→540`、5→S/0→O/1→I…），专治点阵字误读 |
| `_tight_digit_box(engine, img, box_px, digits)` | **标注框收紧到只圈数字**：在颗粒框内重新 OCR，按数字串在行内的位置比例裁出紧致框（去掉前面 SEC 字母），定位失败保留原框。recognize_side/recognize_rules 对已解码颗粒调用 |
| `_vl_fallback_dram(pending, year)` | 把低置信/未读颗粒**逐颗**交大模型识别。**发送前先 `_prep_for_vl` 统一翻正+增强**（所有识别路径共用此唯一入口）；每颗单图单调用 `read_crop_vl`、线程池并发。**透明**：读出后展示值用大模型读数，但**保留** `ocr_raw`/`ocr_confidence`（不覆盖真实 OCR 置信），note 摊开「OCR原文(置信)→大模型读为X」。**不再标「遮挡」**——读不出就原样显示 OCR 原文 |
| `_pick_chip_date(dets, year)` | 从单芯片整图 OCR 结果里挑最可信的 YYWW（序列号前缀略加权），返回 (raw,(y,w),box,text) 或 None |
| `recognize_chip(image_path, kind, current_year=None)` | **主控特写主函数**：整图 `recognize` 后挑最可信 YYWW；识别不到返回 `status="unknown"` 的占位 DateCode（不误报）。返回 `[DateCode]`（0~1 个） |
| `recognize_pcb(image_path, current_year=None)` | **PCB 主函数（双路综合）**：PCB 丝印低对比度，**同时**用 PaddleOCR 与 Qwen-VL 大模型识别再综合——两者一致→采用；只有一方→采用那个；不一致→取大模型并标冲突待人工确认；都没读出→未识别。**大模型自评置信度**写入 `DateCode.model_confidence` 并在 source_text/note 中给出，前端展示「大模型置信 X%」徽标。返回 `[DateCode]` |

### app/recognition/visualize.py — 标注绘制

用 PIL + 中文字体把识别框和标签画到图上。`TYPE_COLORS`（类型→颜色）、`RAW_COLOR`（橙，原始读数）、`EMPTY_COLOR`（灰，无文字）、`_FONT_CANDIDATES`（Windows 中文字体候选）。

| 函数 | 作用 |
|---|---|
| `_load_font(size)` | 按候选路径加载中文 TrueType 字体，失败回退默认字体 |
| `_poly_bbox(box)` | 多边形外接框 |
| `_label_and_color(c, type_color)` | 决定标签与颜色：解码成功→「YY年WW周」(类型色)；未解码→OCR 原始读数(橙)；无读数→「未识别」(灰) |
| `annotate(image_path, codes, out_path)` | 简单标注：每个框画多边形 + 框旁贴「类型｜年W周」标签 |
| `_rects_overlap(a, b, pad=2)` | 两个标签矩形是否重叠 |
| `_place_column(items, x_inner, side, top_guard, img_h, gap)` | 在某一侧边距把标签竖向堆叠、互不重叠，返回带避让矩形与引线锚点的列表 |
| `annotate_clean(image_path, codes, out_path, title="")` | **主标注函数**：先量标签、按框中心分左右；**两侧空白不够时自动扩出面板画布**（内存条贴边也不会截断标签）；顶部标题条；画框（解码=类型色，未解码=橙）；标签排到两侧竖向避让、引线连回各框。清晰、不重叠、不裁切 |

### app/inspection/quality_inspect.py — 外观质检（Qwen-VL）

把正/背面照片 base64 内嵌发给通义千问 VL（DashScope OpenAI 兼容接口），强约束只回 JSON，再由代码做最终红/绿灯判定。**密钥与模型名从项目根目录 `.env` 读取，源码中不再硬编码 key。**

模块级常量：`_API_BASE`（DashScope 北京区）、`_TIMEOUT`、`_PROMPT`（质检提示词，规定检查项与 JSON 输出结构）。

| 函数 | 作用 |
|---|---|
| `_load_dotenv()` | 免依赖读取根目录 `.env`，把 `KEY=VALUE` 注入环境变量（不覆盖已存在的真实环境变量）；模块导入时调用一次 |
| `_model()` | 返回质检模型名（环境变量 `QWEN_VL_MODEL`，默认 `qwen3-vl-235b-a22b-instruct`） |
| `_api_key()` | 返回 DashScope 密钥（环境变量 / `.env` 的 `DASHSCOPE_API_KEY`），未配置返回空串 |
| `read_yyww_vl(image_path)` | 用 Qwen-VL 从 PCB/芯片特写里读 4 位 YYWW 日期数字 **+ 大模型自评置信度**；返回 (digits4, 原文, confidence 0~1)，缺 key/异常优雅回退。供 `recognize_pcb` 与 PaddleOCR 综合 |
| `read_crop_vl(crop, kind="dram")` | 把**单颗**芯片日期码小图交 Qwen-VL 读数字（单图单调用，比批量更准、不相互串位）；`recognize_side` 逐颗兜底用，线程池并发。缺 key/异常返回空串 |
| `read_label_vl(image_path)` | 用 Qwen-VL 读正面标签，返回 `{brand, model, frequency, sn}`（每根 SN 不同，从标签读，可人工改） |
| `appearance_bools(parsed)` | 把外观结果归纳成三态 bool（comp_ok/gold_finger_ok/chip_mark_ok）+ 各自不合格说明 `fails` |
| `_data_url(path)` | 把图片读成 `data:image/...;base64,...` 内嵌 URL |
| `_extract_json(text)` | 从模型回复里抽出 JSON（容忍 ```json 包裹/前后多余文字，退而截取首尾大括号） |
| `_chat(content)` | **大模型调用中枢 + 降级容错**：按 `settings_store.ordered_providers()`（启用的排第一，其余跟随）逐个 provider 试 OpenAI 兼容 `/chat/completions`，某个失败(网络/欠费/超时/非200/异常)就记日志并**自动切下一个**，全失败才抛错；成功累计 `metrics.add_vl_usage` |
| `_call_qwen(image_paths, prompt)` | 组装多模态消息（文本 + 多张图）→ `_chat` 发送（含降级容错） |
| `_prov()/_base()/_model()/_api_key()/_timeout()` | 从 `settings_store` 当前启用 provider 取配置（回退 .env） |
| `_verdict(parsed)` | 据模型结构化结果做最终判定：元器件损坏/发黑、金手指异常、任一芯片标记无线条 → 不合格，汇总原因列表。返回 (合格?, reasons) |
| `inspect_module(front_path=None, back_path=None)` | **对外主函数**：至少一张图 → 调模型（仅模型调用计时 `model_sec`）→ 解析 → 判定，返回 `{ok,status,qualified,reasons,details,model,model_sec[,error]}` |

### app/storage/db.py — MySQL 持久化

质检记录 + 操作人存 MySQL。配置从 `.env` 读（`MYSQL_HOST/PORT/USER/PASSWORD/DB`），首次启动自动建库建表。

| 函数 | 作用 |
|---|---|
| `init_db()` | 建库 + 建表（`operators`/`inspection_records`/`audit_log`），幂等。**增量迁移**：`_ensure_columns` 查 information_schema，缺列即 `ALTER ADD`（**不 DROP、改表不丢数据**）；预置操作人。启动时调用 |
| `_ensure_columns(cur, table, cols)` | 表已存在时缺哪列补哪列（列定义见 `_RECORD_COLUMNS`）；自愈任意历史 schema 漂移 |
| `list_operators()` / `add_operator(name)` / `delete_operator(name)` | 操作人增删查（前台可选/可改） |
| `save_record(rec)` | 保存一条质检记录（含 4 个图片列），返回自增 id；同一 SN 每次**新增一行**留历史；每存一条调 `metrics.record_verdict` |
| `update_review(id, status)` | 更新人工复查状态（未复查/复查合格/复查不合格） |
| `list_records(limit=50)` | 倒序取最近记录（storage_chips JSON 解析、时间格式化、含图片路径） |
| `add_audit(operator, action, target, detail)` / `list_audit(limit)` | 审计日志读写（改复查、删模板/操作人 等敏感操作）；`GET /api/audit` |

表 `inspection_records`：`created_at`、`operator`、`sn`(可重复留历史)、`brand/model/frequency`、`controller_date`/`pcb_date`(CHAR6=YYYYWW)、`storage_chips`(JSON,含 idx)、`storage_count`、`comp_ok/gold_finger_ok/chip_mark_ok`(三态)、`date_ok`、`verdict`、`fail_desc`、`review_status`，**+ 追溯图片 `front_img/back_img/annotated_front/annotated_back`**（`/archive` URL）。
表 `audit_log`：`created_at/operator/action/target/detail`。
**追溯归档**：`services.archive_record_images` 把 `/uploads`、`/outputs` 引用的原图+标注图复制到永久区 `archive/<日期>/<uid>/`（`server` 挂 `/archive` 静态），记录存 `/archive` 路径——uploads 被清也能回看。`scripts/backup_db.bat` 每日 `mysqldump` 到 `backups/`（留 30 天）。

### app/server.py — FastAPI 入口（~90 行）

只负责**组装**：`ocr_engine.configure(...)`（按环境变量）→ 建 `app` → `db.init_db()` → 挂 `/outputs`、`/uploads` 静态 → 定义 `GET /`、`/logo.png`、`/favicon*`、`/api/health` → `app.include_router(...)` 注册四个 router。**不含业务逻辑与具体接口**。

> 注：FastAPI 0.138+ 的 `include_router` 把每组路由包成一个惰性 `_IncludedRouter`（请求时分发），故 `app.routes` 里看不到摊平的条数——要验证路由请**实发 HTTP 请求**，别数 `app.routes`。

### app/logging_setup.py · app/metrics.py — 运维基座（Phase 1）
- `logging_setup.setup_logging()`（server 启动即调，幂等）：分级日志 + `TimedRotatingFileHandler` 写 `logs/app.log`（午夜滚动、留 14 天）+ 控制台；`_ErrorCounter` 把 WARNING+ 计入 `metrics`；关掉 uvicorn 自带逐条 access（由 server 中间件统一记，带耗时+降噪）。环境变量 `LOG_LEVEL`/`LOG_KEEP_DAYS`。
- `metrics`：内存计数（`records_total/pass/fail/unknown/errors/uptime/yield`）；`db.save_record` 每存一条调 `record_verdict`；`snapshot()` 供 `GET /api/metrics`。重启清零。
- **进程守护**（`scripts/`）：`run_server.bat`（`:loop` 崩溃自动重启 uvicorn）；`service_install.ps1`（注册任务计划**开机自启**，零依赖；管理员 `powershell -ExecutionPolicy Bypass -File scripts\service_install.ps1`，加 `-Uninstall` 卸载）。
- 健康：`GET /api/health` → `{status, version, db_ok, hik_cameras, dashscope_key, templates, slots}`；`GET /api/metrics` → 上述指标。

### tests/ · tools/eval_accuracy.py — 测试与评测（Phase 3）
- pytest 单测（**39 例**，0.4s，运行 `.venv\Scripts\python.exe -m pytest`；根 `conftest.py` 保证 `import app`，dev 依赖见 `pyproject.toml`）：
  - `test_date_parser.py`：日期解码（YWW/YYWW、世纪、非法周、YYYYWW 归一）。
  - `test_compute_signal.py`：**综合判定**（逐颗比较，含"单颗被换必判不合格并定位"铁律、阈值边界、盲点单列）。
  - `test_slot_occupancy.py`：**托盘空位检测 + 标签/白区框位判别**（边缘密度判空/满、显式 slots 优先/自动聚类、框→槽归属、`_box_kind` 白区→label/密纹→chip）。
  - `test_stick_split.py`：**一盘拆 N 条 · 逐根判定**（跨根不比较、不同批次两根各自合格；根内混入被换芯片只该根 fail 并定位；盲点按根隔离）。
  - `test_dedup.py`：**防重复放盘**（4 根 SN 指纹顺序无关、有效SN<2不去重、本批同盘跳过、换条不重复、新批清零）。
  - `test_sn_history.py`：**同 SN 历史比对**（历次日期变了→changed 告警、集合比顺序无关、单条不告警）。
- `tools/eval_accuracy.py`：给"样本目录 + 每根 `truth.json`(`expected_yyyyww`)"→ 复用 `services.run_recognize` → 输出每颗读对率/错在哪颗/整条判定是否符合预期。用法 `python tools/eval_accuracy.py <目录> [--mode rules|template] [--template <id>]`。
- `tools/build_template.py`：**从一张多根照片自动生成整图固定模板**（recognize_rules 自动框每颗 → 按 x 聚成 N 根 → 写 `slots`，存 template_store）。用法 `python tools/build_template.py <图> --side front|back --id <模板id> --sticks 4 [--merge]`。真机拍到一盘 4 根正/反照片后各跑一次即出模板；之后 `analyze_and_save(mode=template)` 一次识别 4 根、拆 4 条入库。实测 demo1(4根)：自动出 4 槽模板、模板模式识别 stick_count=4、每根 20 颗各自 pass。
- `tools/calibrate_slots.py`：**托盘空位阈值标定**。用法 `python tools/calibrate_slots.py <模板id> <front|back> <图片>`——打印每个槽的 presence 分数与空/满判定；满盘/空盘各拍一张，据此把 `SLOT_PRESENCE_MIN` 设到两者中间。

### app/prompts.py — 大模型提示词集中管理
所有发给多模态大模型的提示词都在这一个文件，**调词只改这里、不碰识别逻辑**：固定词 `APPEARANCE`(外观质检)/`PCB_DATE`(PCB日期)/`LABEL`(读标签) 是常量字符串；带变量的 `crops(n, want)`(批量颗粒)/`crop(want)`(单颗兜底) 是函数（f-string，JSON 花括号写 `{{ }}`）；`want_text(kind)` 给位数说明。`quality_inspect` 从这里取。

### app/core.py — 共享配置
路径常量（`BASE_DIR/UPLOAD_DIR/OUTPUT_DIR/WEB_DIR/TEST_DIR/WATCH_DIR`，import 时自动 `makedirs`）、`SLOT_LABELS`（四槽中文名）、`SLOT_KIND`（front/back=side 固定框，pcb/controller=chip 整图）、`SPREAD_THRESHOLD_WEEKS=10`（阈值默认值，可被前端 `threshold` 覆盖）、`IMG_EXT`。

### app/services.py — 识别/质检 服务层
被各 router 复用的业务逻辑（从原 server.py 抽出）：

| 函数 | 作用 |
|---|---|
| `resolve_set(folder)` / `_match_slot(fname)` | 把一个文件夹的图片解析成 `{slot: 路径}`（按文件名关键字，再按 front→back→pcb→controller 补位） |
| `save_upload(uf, uid, slot)` | 上传文件落盘到 `uploads/{uid}_{slot}.ext` |
| `_counts` / `_make_title` / `_week_ordinal` / `_loc_label` / `_blind_dram` | 统计、标注标题、周序号、异常定位标签、盲点颗粒 |
| `compute_signal(codes, threshold=10)` | **合格信号（逐颗比较，严禁多数表决）**：能读出的日期两两比最大周差，≤thr→pass / >thr→fail / <2 个→unknown；**任一不一致即 fail 并定位到第几颗**（防偷换单颗芯片）。盲点单列 `blind/blind_desc` 提示人工 |
| `_structure_dates(codes, signal)` | 整理入库结构：controller_date/pcb_date/storage_chips(YYYYWW+序号)/date_ok/date_fail |
| `_recognize_core(paths, uid, …)` | 逐图识别+标注（含托盘空位跳过），返回 **DateCode 对象** + 每面元数据(`sides`/`occ_by_side`)，供 `run_recognize`(对外JSON) 与 `analyze_and_save`(按根拆分) 共用 |
| `_stick_breakdown(codes, thr)` | 按 `slot` 分组，**逐根各自** `compute_signal`（同根内逐颗比、不跨根），返回 `sticks:[{slot,pos,signal,dates,counts}]` |
| `run_recognize(paths, uid, mode, …)` | `_recognize_core` + 顶层 `signal`/`dates`(整图汇总，展示用) + `sticks`(逐根判定) + `slots`/`stick_count`。空槽标注图画「空位」灰框、不参与判定 |
| `analyze_all(paths, …)` | **三路并行**（识别 / 外观质检 / 读标签），墙钟≈最慢一路，返回 (rec, insp, label)。仅 `/api/folder/recognize` 展示用 |
| `build_record(rec, insp, label, operator, batch)` | 组装 DB 记录 + 综合判定(verdict)：日期与三态 bool 全 True→pass、任一 False→fail；有 batch 则品牌/容量/频率/客户/单号以批次登记为准 |
| `_crop_slot(src, box_px, tag)` | 把某槽区域从原图裁出存 uploads，供该根**单独**送大模型读 SN/外观 |
| `analyze_and_save(job, …)` | 一盘内存条入库。**托盘 ≥2 根**：按槽位**拆成 N 条独立记录**——每根各自逐颗判定(不跨根)、各自读 SN、各自外观、各自归批，`slot_pos` 记托盘第几槽；返回 `multi/sticks[]` + 聚合。单根/规则模式仍整图一条 |
| `tray_fingerprint(sns)` / `_is_dup_tray(batch_id,fp)` | **防重复放盘**：一盘 4 根 SN 去重排序做指纹；本批(batch_id 作用域)已见同指纹 → `analyze_and_save` 返回 `duplicate:True` 不重复入库。有效 SN<2 不去重(避免空 SN 误判)，新批次天然清零 |
| `sn_history_diff(records)` | **同 SN 历史比对(防偷换第二道)**：比各次颗粒日期集合/主控/PCB，变了则 `changed:True` 告警 |
| `_read_label(front)` | 读一根标签：**先解标签二维码**([app/recognition/barcode.py](app/recognition/barcode.py) `read_label_code`，zxing-cpp 解 DataMatrix→精确 SN/型号/规格)，解不出再 `read_label_vl` 大模型兜底。4 根同图时对各槽裁图逐根解、各归各根。依赖 `zxing-cpp`(uv pip install) |
> **标签二维码字段**：`(S)`→SN · `(P)`→型号 · `(L)`→规格(抽出容量/频率) · **品牌由型号前缀推断**。二维码全部字段入库(inspection_records 增列 `spec`完整规格/`mfg`厂商批次码,连同 sn/brand/model/frequency/capacity)(M3xx/M4xx=Samsung、MT=Micron、HM=SK Hynix 等)。SN/品牌/型号/频率 均由二维码解码得到，前端识别后自动回填、入库亦以此为准(无批次登记时)。


### app/routers/ — 接口分组（APIRouter）

| router | 接口 |
|---|---|
| `recognition.py` | `POST /api/recognize`（识别主接口，四槽至少一个；pcb 走双路综合，rules/template 两模式）·`POST /api/inspect`（外观质检）·`GET /api/templates`·`DELETE /api/templates/{id}`·`POST /api/read_label`·`GET /api/folder/list`·`POST /api/folder/recognize` |
| `cameras.py` | `GET /api/camera/status`·`GET /api/camera/preview/{side}`·`POST /api/camera/capture`（UVC）；`GET /api/hik/status`·`GET /api/hik/preview`·`GET·POST /api/hik/exposure`·`POST /api/hik/capture`·`POST /api/hik/capture_both`（海康双相机，接口带 `side=front|back`） |
| `records.py` | `GET·POST·DELETE /api/operators[/{name}]`·`GET·POST /api/records`·`POST /api/records/{id}/review`·`GET /api/records/by_sn/{sn}`(SN 追溯+历史比对)·`GET /api/records/export?format=xlsx\|csv&筛选`(报表导出，**25 列全字段**：时间/SN/客户/批次/品牌/型号/容量/频率/规格/厂商/成色/槽位/主控日期/PCB日期/颗粒数/颗粒日期明细/元器件/金手指/芯片标记/日期一致/判定/说明/复查/操作人/备注；外观三项与判定中文化、颗粒明细文字化) |
| `pipeline.py` | `POST /api/pipeline/start·stop`·`GET /api/pipeline/status·stream(SSE)` |
| `batches.py` | **批次登记 + 良率统计**：`POST/GET /api/batches`·`POST /api/batches/{id}/close`·`GET /api/stats/overview`(今日/累计良率)·`GET /api/stats/customers`(按客户) |
| `auth.py` | **登录鉴权（受控准入）**：`POST /api/register`(自助注册→待审核)·`POST /api/login`(校验+审核通过才发会话)·`POST /api/logout`·`GET /api/me`(当前用户)·`GET /api/users`(管理员)·`POST /api/users/{id}/review`(审核 approve/reject)·`POST /api/users/{id}/reset_pw` |

**登录鉴权（Phase 5）**：新人在入口页 **`GET /login`**（Apple 风极简页：中央动态**橙↔蓝圆点环**呼应 logo + 登录/注册切换；注册填 用户名(姓名)/手机号/密码，不含分公司·部门）自助注册 → 状态 `pending`；**管理员**在 **`GET /users`** 审核通过后才能登录。角色分 **admin / operator**：质检员只能用工作台 `/`，`/manage`·`/settings`·`/users` 及 `/api/users*` 仅管理员。实现:密码 `pbkdf2_hmac` 加盐哈希(不存明文)、会话 token 存 `sessions` 表 + **HttpOnly cookie `sid`**(同源自动带,前端 fetch 无需改)、[app/server.py](app/server.py) 中间件统一门禁(未登录→页面跳 `/login`/接口 401)。种子超管由 `.env` 的 `ADMIN_USER`/`ADMIN_PASSWORD` 首启自动创建。全部标准库,**零新增第三方依赖**。表:`users`(账号/pw_hash/salt/role/status/**phone 手机号**) + `sessions`。登录页高清 logo 走 `GET /logo_hd.png`(topbar 仍用小 `logo.png`)。

**批次登记（Phase 4）**：每批测试前登记 `客户/品牌/容量/频率/批次号`（`batches` 表）；前端左栏「当前批次」卡登记/选择。之后每根质检**归入当前批并继承这些信息，只读 SN**（`services.build_record(...batch=)`、`analyze_and_save(...batch_id=)`）。良品率看板独立页 **`GET /manage`**（`web/manage.html`）：今日/累计良率 KPI + 按批次/按客户表 + 记录明细（判定/说明/归档图/复查）。`verdict='pass'` 计为良品。

### app/templates/ — 型号模板数据目录

每个型号一个 `<id>.json`：`{id, brand, model, note, created, sides:{front:{image_size:[W,H], boxes:[{type, box(归一化4点), manual, id}]}, back:{...}}}`。首个/默认模板 `samsung-m321r8ga0pb0.json`（Samsung M321R8GA0PB0-CWMKJ · DDR5 RDIMM 64GB）**按 demo3 手工标注的每颗日期位置**生成：正面 16 框（15 dram + 1 controller 主控，识别时跳过）、背面 20 dram。归一化坐标与分辨率无关，适用于同款、取景相近的照片。`default_template_id()` 取列表首个，故未选模板时即用它。**将来用 PaddleOCR 自动框生成的 JSON 直接放入本目录即被列出/可选。**

### web/index.html — 单页前端

一页含：顶部**综合判定单灯**（外观+日期都合格才绿）、识别方式/模板下拉、**合格阈值输入（最大周差，可调）**、四个上传槽、分项计时、解码结果与汇总、「外观质检」明细区（正反面上传齐后自动质检）、型号模板管理卡。

| JS 函数 | 作用 |
|---|---|
| `verdictState` / `renderVerdict()` | 维护「日期」「外观」两项的合格状态与问题列表；渲染**唯一**的综合判定灯：两项都 done 且无问题→绿，任一有问题→红并列出全部问题，未齐→进行中 |
| `setDateVerdict(d)` | 把日期结构化结果（`d.dates.date_fail` 定位说明）写入 verdictState.date |
| `readLabel(f)` | 上传正面后自动 `POST /api/read_label`，把 品牌/型号/频率/SN 填入输入框（可人工改） |
| `bindSlot(side)` / `setFile(side,f)` | 绑定识别槽的点击/拖拽上传，预览缩略图，控制「开始识别」按钮可用；正反面齐时触发质检 |
| `go.onclick` | 收集四槽文件 + 选中的 `template_id` POST `/api/recognize`，调 `render` |
| `statusLabel(c)` | 渲染单元格：颗粒带 `#序号`；解码→日期；未解码→「原文 …」(橙)；无读数→「未识别」 |
| `render(d)` | 渲染分项计时（日期识别用时/总耗时）、各面标注图 + 逐项明细表、解码汇总表 |
| `maybeRunInspect()` / `runInspect()` | 正面+背面都齐时自动 POST `/api/inspect`（同一对图不重复跑） |
| `renderInspect(d)` | 把外观合格状态写入 verdictState.inspect（喂顶部综合判定），并在质检卡内展示**多模态大模型用时**、不合格原因列表、可展开的模型明细表（本卡不再单独亮灯） |
| `yn(b)` / `yn2(b)` | 把布尔渲染成「是/否」（异常标红的两种语义） |
| `loadTemplates()` | 拉 `GET /api/templates` 填充下拉与模板管理列表（页面加载时调用） |
| `deleteTpl(id)` | 确认后 `DELETE /api/templates/{id}`，刷新列表 |
| `loadOperators()` | 拉 `/api/operators` 填右上角操作人下拉（localStorage 记住上次选择）；＋新增 / ×删除 |
| `save-record` 按钮 | 组装新版记录 {操作人, SN, 品牌/型号/频率, controller_date/pcb_date, storage_chips, 三态 bool, verdict, fail_desc(仅不合格)} → `POST /api/records` 存档 |
| `loadRecords()` | 拉 `/api/records` 渲染「质检记录」卡（SN/品牌型号/结果+说明/日期/外观/**人工复查下拉**/操作人时间） |
| `setReview(id, status)` | 记录行的人工复查下拉变更 → `POST /api/records/{id}/review` |

### 根目录脚本与配置

| 文件 | 作用 |
|---|---|
| `run.bat` | Windows 启动脚本：设 UTF-8、默认 GPU，启动 uvicorn |
| `pyproject.toml` / `.python-version` | 项目元数据 / Python 版本（3.12，因 paddle 暂不支持 3.14） |

> 已移除一次性/测试脚本（旧 `recognize_37.py`、`test_region.py`、`tests_date_parser.py`、`build_layout.py`）与单一布局 `box_layout.json`（已迁入模板库）。**未来计划**：用户自写脚本读某文件夹照片 → PaddleOCR 自动框 → 调 `template_store.save_template()` 或直接写 JSON 到 `app/templates/`，即批量扩充模板库。

---

## 三、运行

    run.bat
    或: .venv\Scripts\python.exe -m uvicorn app.server:app --host 127.0.0.1 --port 8000

浏览器打开 http://127.0.0.1:8000 ，操作流程：
1. **选识别方式**：「规则识别（整图自动找日期，默认）」或「模板识别（按型号固定框）」。选模板识别时再选品牌/型号模板。
2. 在四个槽位上传图片：**正面颗粒 / 背面颗粒 / PCB 板 / 主控芯片**（规则识别下每张图都会被整图扫描找日期）。
3. **外观质检自动进行**：当「正面」「背面」两张都上传后，前端自动把这两张发给 Qwen-VL 质检，「④ 外观质检」处亮红/绿灯并给原因 + **多模态大模型用时**。
4. 四张传完后点**「开始识别」**，看标注图、逐处原生识别结果（解码出的日期 或 OCR 原始读数）、**日期识别用时**。顶部**综合判定灯**汇总外观+日期：两项都合格才绿，否则红并列出问题。
5. 「⑤ 型号模板管理」可查看/删除模板。

### API

`POST /api/recognize`（multipart，四文件均可选，至少一个）
- `front` / `back`：正/背面原图（按模板固定框只识别颗粒，原生结果不预测）
- `pcb` / `controller`：PCB 板、主控芯片特写（整图识别）
- `current_year`：年份基准（可选，3 位码靠它判断年代）
- `threshold`：合格阈值（可选，最大周差 ≤ 该值为合格；缺省 10，前端可调）
- `mode`：`rules`（默认，整图规则识别，不用模板）或 `template`（按模板固定框）
- `template_id`：模板识别时用的型号模板（可选，缺省用默认模板）
- 返回：`{ ok, mode, elapsed_sec, ocr_sec, template_id, total, signal{...}, sides[...], summary }`

`POST /api/inspect`（multipart，外观质检；`front`/`back` 至少一个）—— 前端在正反面上传齐后自动调用
- 返回：`{ ok, status, qualified, reasons[], details{components,gold_finger,chip_marks,issues}, images, model, model_sec, elapsed_sec }`

`GET /api/templates` → `{ templates:[{id,brand,model,note,created,counts}], default }`
`DELETE /api/templates/{id}` → `{ ok, deleted }`
`GET /api/health` → `{ status, slots:["front","back","pcb","controller"], templates:N }`

### 环境变量
- `OCR_DEVICE=gpu|cpu`（默认 gpu）
- `OCR_SERVER_MODELS=1` 服务端高精度模型（默认开）；`=0` 用轻量模型
- `OCR_DET_SIDE_LEN=1536`（检测分辨率，越大小字越准越慢）
- `OCR_LANG=en`（默认）
- `OCR_CONF_MIN=0.85`（颗粒 OCR 置信度阈值：低于它或未解码的颗粒改用大模型识别）
- `SLOT_PRESENCE_MIN=0.045`（托盘槽位占位阈值：槽内边缘像素占比 ≥ 它判"有条"，否则空位跳过。真机用满盘/空盘各拍一张跑 `tools/calibrate_slots.py` 标定）

外观质检的密钥/模型从项目根目录 **`.env`** 读取（`app/inspection/quality_inspect.py` 导入时自动加载）：
- `DASHSCOPE_API_KEY=sk-...` 通义千问密钥（**必填**，源码不再硬编码；缺失则质检返回错误）
- `QWEN_VL_MODEL=qwen3-vl-plus`（外观/PCB/标签 用的多模态模型，默认取速度更快的 plus；要更高精度可换 `qwen3-vl-235b-a22b-instruct`）
- `QWEN_BASE_URL`（默认 DashScope 北京区 OpenAI 兼容地址，可选）

MySQL 质检记录持久化也从 `.env` 读：
- `MYSQL_HOST`（默认 127.0.0.1）、`MYSQL_PORT`（3306）、`MYSQL_USER`（root）、`MYSQL_PASSWORD`、`MYSQL_DB`（默认 yunxiaoquan_qc，自动建库）

相机采集（服务器双相机）：
- `FRONT_CAM_INDEX`/`BACK_CAM_INDEX`（设备序号，默认 0/1）、`CAM_WIDTH`/`CAM_HEIGHT`（采集分辨率）、`CAM_PREVIEW_WIDTH`（预览缩放宽）

`.env` 示例：
```
DASHSCOPE_API_KEY=sk-xxxxxxxxxxxxxxxx
QWEN_VL_MODEL=qwen3-vl-235b-a22b-instruct
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=123456
MYSQL_DB=yunxiaoquan_qc
FRONT_CAM_INDEX=0
BACK_CAM_INDEX=1
CAM_WIDTH=1920
CAM_HEIGHT=1080
```

---

## 相机采集（服务器端双相机，UVC）

面向流水线：**两台 UVC 相机插在服务器上**（正/背面各一台），后端用 OpenCV 直接读本机相机，
任意电脑用浏览器访问服务器地址即可看实时预览、点「拍照识别」——**浏览器不碰相机，无需 HTTPS**。

- [app/cameras/camera.py](app/cameras/camera.py)：相机 Hub。每台相机一个常驻抓帧线程存"最新帧"；
  预览(MJPEG)与拍照共用最新帧。相机缺失/打不开时**优雅降级**（不影响手动上传）。
  函数：`status()`（各路是否可用）/ `snapshot(side, path)`（全分辨率抓一帧存盘）/ `mjpeg(side)`（预览流）。
- 手动触发（当前）：放好内存条 → 点「拍照识别」→ 服务器抓正/背面两帧 → 识别 + 外观质检 + 读标签 → 出结果。
- 接口：
  - `GET /api/camera/status` → `{front, back}` 是否可用
  - `GET /api/camera/preview/{side}` → MJPEG 实时预览流（浏览器 `<img>` 直接看）
  - `POST /api/camera/capture` → 抓两帧并识别，返回 `{ok, recognize, inspect, label}`
- 配置（`.env`）：`FRONT_CAM_INDEX`/`BACK_CAM_INDEX`（设备序号，默认 0/1）、`CAM_WIDTH`/`CAM_HEIGHT`（采集分辨率，小字建议拉高）、`CAM_PREVIEW_WIDTH`（预览缩放宽度）。
- 依赖：`opencv-python`（cv2）。
- 前端：识别卡顶部「📷 相机采集」面板（两路实时预览 + 拍照识别）；**无相机时自动隐藏**，回退到手动上传四槽。

> 部署：相机插服务器、服务跑服务器；质检员/主管用**任意电脑** `http://服务器IP:端口` 访问。
> 一台相机翻面 → 只接一路；此处按**两台相机同拍正反**实现。

## 海康工业相机（Hikrobot MVS SDK，双相机 上/下 GigE）

面向真实产线：**两台海康工业相机**（MV-CS050-10GC，GigE）**上下对置**，托盘居中，**一次同时拍正反两面**（不翻盘）。按**相机序列号**绑定角色（不靠插口顺序）：**上=正面(front)=SN DB1224590、下=反面(back)=SN DB1623157**。

- [app/cameras/hik_camera.py](app/cameras/hik_camera.py)：封装 MVS Python SDK（自动读 `MVCAM_COMMON_RUNENV` 定位 `Samples/Python/MvImport`）。
  - `enum_devices()` / `status()`：枚举已连相机（型号/SN/IP）+ `roles`（角色→SN 绑定）。
  - `HikCamera(sn=…)`：按 SN 打开某台；`open/snapshot/grab_array/mjpeg/set_exposure/set_gain/close`。**每台一个独立句柄、独立锁，可并行**。
  - **角色注册表**：`get_camera(role)`（front/back，按 SN 懒加载）；模块级 `snapshot/mjpeg/set_exposure/…` 都带 `role=` 参数。
  - `capture_both(front_path, back_path)`：**两线程并行、同时抓上下两台**（防正反错位），实测 ~0.8s 出两张 2448×2048 图。
  - SDK 未装/相机未连/被占用时**优雅报错**，不影响系统其余部分。
- 接口（`side=front`(上)/`back`(下)）：
  - `GET /api/hik/status` → `{sdk, devices:[{model,sn,ip}], roles:{front,back}}`
  - `GET /api/hik/preview?side=front|back` → 某台 MJPEG 实时预览
  - `POST /api/hik/capture`（`side`,`uid`）→ 某台抓一帧存 uploads（补拍用，仍存扁平 `uploads/{uid}_{side}.jpg`）
  - **`POST /api/hik/capture_both`（`uid`）→ 双相机同时抓正+反两张**（托盘工作流用）；两张统一命名 `front.jpg`/`back.jpg`，存进**新建的有序子文件夹** `uploads/<序号>/`（序号 0001、0002… 由现有最大序号+1，代表拍摄先后），返回 `{seq, front_url:/uploads/<序号>/front.jpg, back_url:…}`
  - **`POST /api/hik/capture_and_save`（`mode/template_id/threshold/batch_id/operator`）→ 一键：同时拍正反 → 识别 → 拆 N 条入库**（内部：新建 `uploads/<序号>/` 存 front/back → `services.analyze_and_save` **就地识别、不再复制成扁平图**，保持 uploads 根整洁）；工作台「同时拍正反 → 识别入库」按钮调它
  - **静止即拍（自动触发，手动仍保留）**：`POST /api/hik/auto/start|stop`、`GET /api/hik/auto/status`。[app/pipeline/motion_trigger.py](app/pipeline/motion_trigger.py) 后台盯上相机预览帧，帧差状态机 `待机→(放盘运动)→等静止→(连续N帧静止)拍→已拍锁定→(取盘运动)重新武装`；触发即调 `capture_and_save`，结果经 `runner.push_result` 推现有 SSE（工位屏/工作台可见）。阈值 `.env`：`AUTO_MOTION_THR/AUTO_STILL_THR/AUTO_STILL_FRAMES/AUTO_COOLDOWN/AUTO_POLL`。实测：启动不误触发(静态 idle)、可停
  - `GET/POST /api/hik/exposure`（`exposure_us`,`side`）→ 读/设某台曝光(微秒)；`GET/POST /api/hik/gain`（`gain_db`,`side`）→ 读/设某台增益(dB)。**上/下两台曝光·增益各自独立**（光照不同分别调），前端双预览下每台一组控件；`GET/POST /api/hik/orient`（`side`,`mode`=none/fliph/flipv/rot180）→ **反面方向校正**：上/下相机对拍，反面相对正面是左右镜像，默认 `back=rot180` 翻正(反面相机180°装,rot180 让字转正可读+左右对齐;fliph会把字镜像成反字读不了)，让正/反同一根对齐（预览+抓拍统一生效，真盘到货可一键改）
  - **带宽/稳定性调优**（两台共用一条千兆线，`.env` 可调）：`HIK_FPS`(采集帧率，**当前15**；双相机各走独立网卡后可跑高些，共用一条线时降到10更稳) · `HIK_PREVIEW_FPS`(预览帧率，**当前8**) · `HIK_GEVSCPD`(包间延迟ticks，默认0关) · `HIK_THROUGHPUT_BPS`(每台吞吐上限Bps，默认0不限) · **`HIK_PACKET_SIZE`**(GigE 包大小字节，**默认1500=标准帧最稳**；0=按网卡MTU自动探测。**经验**：front 走的 2.5G/巨帧网卡自动探测出大包，但巨帧要端到端全链路都稳定支持，链路稍不稳就整包丢→**预览花屏/撕裂**；强制1500后 front 预览花屏消失，back 本就1500) · **`HIK_RESEND`**(GigE丢包重传，**默认1开**：SDK发现丢包→请求相机重发→拼完整帧，消除花屏；`MV_GIGE_SetResend`，须在取流前设) · **`HIK_FRONT_EXPOSURE_US`/`HIK_FRONT_GAIN`/`HIK_BACK_EXPOSURE_US`/`HIK_BACK_GAIN`**(上/下相机曝光µs·增益dB，开机自动应用——防相机断电回默认5ms导致全黑、静止即拍/识别失灵)。**back 反面是黑 PCB 反光低、天生偏暗**，已把 back 曝光/增益调高补偿(实测证明是档位不够、非镜头光圈问题；当前 .env back≈85ms/9dB)。抓拍取帧失败会**自动关-重开重试**(掉线自愈)；预览流不自愈(交前端重连)。**注意**：接相机的网卡须为 **1Gbps**——降到 10/100Mbps 会导致取帧超时/掉线（换好网线/换口）。`/api/hik/status` 会返回 `net`(网卡名/速度/warn)，工作台在 <1Gbps 时**红字告警**；预览掉线前端**自动退避重连**(2→4→8s)
- 配置（`.env`）：`HIK_FRONT_SN`/`HIK_BACK_SN`（角色→SN 绑定，默认即上述两台）；`HIK_EXPOSURE_US`/`HIK_GAIN` 开机应用。
- **重要**：每台相机同一时刻只能被一个程序独占——用本系统前，先在**海康 MVS 客户端断开相机 / 关闭 MVS**，否则打不开（`0x80000203`）。GigE 带宽：快照式(每盘一拍)无压力；若两台同时连续取流走同一网口需降帧或开巨帧。

## 三种采集方式（前端可切换）

识别卡顶部一个分段开关，测试期三种都在：
1. **📷 相机采集**：服务器双相机（无相机时该选项自动禁用）。
2. **⬆️ 手动上传**：正/背面颗粒 + PCB/主控特写 四槽拖拽上传。
3. **📁 读取文件夹**（测试用）：直接读服务器 `test_photos/` 下的照片，免上传免相机。
   - 约定：**每根内存条一个子文件夹**，文件名含 `front`/`back`/`pcb`/`controller`（或"正/背"）；也可把散图直接放根目录。
   - 匹配不到关键字时，按 front→back→pcb→controller 顺序补齐。
   - 接口：`GET /api/folder/list`（列出可识别的组）、`POST /api/folder/recognize`（`name`=子文件夹名/`__root__`）。
   - 相机与文件夹都返回 `{ok, recognize, inspect, label}`，前端一次渲染识别+质检+标签自动填。

## 自动识别（托盘拍照 → 目录监听）

> **上料方式：托盘固定位（一盘 4 根）+ 每盘拍照。** 电机传送 / 弹仓 / 到位信号双向握手（含 SimulatedFeeder、SerialFeeder）**已全部弃用并删除**。

托盘拍完写入相机输出目录 → 系统自动识别入库，代码结构：
- **[app/pipeline/feeder.py](app/pipeline/feeder.py)**：`FeederController` 接口 + `FolderWatchFeeder`——监听 `WATCH_DIR`（相机输出目录），发现**新子文件夹且正反两张写完**（大小稳定+可解码）就自动取出，落 `.done` 防重复。
- **[app/pipeline/runner.py](app/pipeline/runner.py)**：`PipelineRunner` 后台循环 `wait_ready → process_fn → send_done(落.done)`，带订阅推送（SSE）与状态。
- **[app/services.py](app/services.py)**：`analyze_and_save(job)`（拷图→识别+质检+读标签→组装记录→归批入库）。
- 接口：`POST /api/pipeline/start`（operator/mode/template_id/batch_id…，**监听 WATCH_DIR 自动识别**）、`/stop`、`GET /status`、`GET /stream`（SSE 实时结果）。
- 前端「自动监听」/工位屏：启动后托盘拍一盘→自动识别→SSE 实时出结果、自动入库。
- 配合**整图固定模板**：一盘 4 根在一张图里全框好，一次识别 4 根（见模板章节）。

---

## 数据库（MySQL 质检记录）

质检结果持久化到 MySQL，用于**留档追溯**。连接配置从项目根 `.env` 读取（见上），
[app/storage/db.py](app/storage/db.py) 在服务启动时 `init_db()` **自动建库、建表**（幂等）；
若检测到旧版 `inspection_records` 表结构（无 `brand` 列），会**自动丢弃重建迁移**。

- 库名：`yunxiaoquan_qc`（`MYSQL_DB`，不存在自动创建）
- 引擎/编码：InnoDB / utf8mb4
- 写入时机：识别完点「保存质检记录」→ `POST /api/records`（前端组装整条记录）。
- 读取：「质检记录」卡 `GET /api/records`（倒序最近 50 条）。

### 表 1：`operators`（操作人）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INT PK AI | 主键 |
| name | VARCHAR(64) UNIQUE | 操作人姓名 |

前台右上角下拉选择，可 ＋新增 / ×删除（`GET/POST/DELETE /api/operators`）；预置「质检员A/质检员B/管理员」。

### 表 2：`inspection_records`（质检记录，18 字段）

```sql
CREATE TABLE inspection_records (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,  -- 质检时间
  operator        VARCHAR(64)  DEFAULT '',   -- 操作人
  sn              VARCHAR(128) DEFAULT '',   -- 内存唯一Id(序列号)，不唯一，同一根多次质检各一行留历史
  brand           VARCHAR(64)  DEFAULT '',   -- 品牌(读标签, 可人工改)
  model           VARCHAR(128) DEFAULT '',   -- 型号(读标签)
  frequency       VARCHAR(32)  DEFAULT '',   -- 频率(读标签, 如 DDR5-5600)
  controller_date CHAR(6)      DEFAULT NULL, -- 主控芯片日期 YYYYWW(如 202517)，未识别 NULL
  pcb_date        CHAR(6)      DEFAULT NULL, -- PCB 日期 YYYYWW，未识别 NULL
  storage_chips   JSON,                      -- 各存储芯片日期数组(见下)
  storage_count   INT          DEFAULT 0,    -- 存储芯片数量
  comp_ok         TINYINT      DEFAULT NULL, -- 元器件是否正常(外观项1；NULL=未检)
  gold_finger_ok  TINYINT      DEFAULT NULL, -- 金手指是否正常(外观项2)
  chip_mark_ok    TINYINT      DEFAULT NULL, -- 芯片二维码标记是否正常(外观项3)
  date_ok         TINYINT      DEFAULT NULL, -- 日期是否合格(最大周差≤阈值)
  verdict         VARCHAR(16)  DEFAULT '',   -- 综合判定: pass/fail/unknown
  fail_desc       VARCHAR(255) DEFAULT '',   -- 不合格说明(逐条"位置:问题"；合格为空)
  review_status   VARCHAR(16)  DEFAULT '未复查', -- 人工复查: 未复查/复查合格/复查不合格
  INDEX idx_sn (sn),
  INDEX idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

**`storage_chips` JSON 结构**（每颗存储芯片一项，`idx` 为标注图上的序号）：
```json
[
  {"idx": 1, "side": "front", "yyyyww": "202534", "status": "ok"},
  {"idx": 2, "side": "front", "yyyyww": "",       "status": "raw"}
]
```
- `idx`：该面从上到下、从左到右的序号（与标注图上「①②…」一致，便于定位）。
- `side`：`front`/`back`。
- `yyyyww`：6 位日期（4 位年 + 2 位周），未解码为空串。
- `status`：`ok`(已解码) / `raw`(未解码，原始读数) / `unknown`(无读数)。

**字段取值约定**
- 日期统一 **YYYYWW**（主控/PCB/每颗颗粒）；未识别存 `NULL`/空。
- 三态 bool（`comp_ok`/`gold_finger_ok`/`chip_mark_ok`/`date_ok`）：`1`=正常/合格，`0`=异常/不合格，`NULL`=未检（如没上传对应图/没做外观质检）。
- `verdict`：**日期 与 三项外观全合格才 `pass`**，任一不合格 `fail`，信息不足 `unknown`。
- `fail_desc`：**仅不合格时**逐条写「位置：问题」，定位到「正面第N颗颗粒 / PCB / 主控 / 元器件 / 金手指 / 芯片二维码标记」；合格为空。
- `sn`/`brand`/`model`/`frequency`：上传正面后大模型读标签自动填（`POST /api/read_label`），页面可人工改，以页面值入库。

### 相关接口
- `POST /api/records`：保存一条记录（JSON body 为上述字段）。
- `GET /api/records?limit=50`：最近记录（`storage_chips` 已解析为数组、时间格式化）。
- `POST /api/records/{id}/review`（form `status`）：更新人工复查状态。
- `GET/POST/DELETE /api/operators[/{name}]`：操作人增删查。

---

## 四、支持的日期码

| 类型 | 位置 | 格式 | 示例 | 解码 | 上传方式 |
|------|------|------|------|------|---------|
| 存储颗粒码 | DRAM 颗粒 | YWW (3位，首位=年份个位) | 534 | 2025 年第 34 周 | 正/背面原图（固定框） |
| PCB 板上码 | 绿色 PCB 丝印 | YYWW (4位) | 2530 | 2025 年第 30 周 | PCB 特写（整图） |
| 主控/RCD 码 | 主控/RCD 芯片 | YYWW (4位，常为序列号前缀) | 2517A0DRCR | 2025 年第 17 周 | 主控特写（整图） |

---

## 五、PaddleOCR 模型文件位置（重要）

模型**不在本项目内**，首次运行自动下载并缓存到用户主目录：

    C:\Users\<用户名>\.paddlex\official_models\

本机即 `C:\Users\Administrator\.paddlex\official_models\`：

| 模型目录 | 用途 |
|---|---|
| PP-OCRv5_server_det | 文字检测（服务端高精度，**当前默认**） |
| PP-OCRv5_server_rec | 文字识别（服务端高精度，**当前默认**） |
| PP-OCRv6_medium_det / _rec | 轻量检测/识别（OCR_SERVER_MODELS=0 时用） |
| PP-LCNet_x1_0_textline_ori | 文本行方向分类 |

每个目录内 `inference.pdiparams` 是权重，`inference.json/.yml`、`config.json` 是结构与配置。首次下载后可离线使用；删目录会在下次用到时自动重下。

---

## 六、环境
- Python 3.12（用 uv 管理 `.venv`，因 PaddlePaddle 暂不支持 3.14）
- paddlepaddle-gpu 3.3.1 (CUDA 12.6) / paddleocr 3.7.0 / requests（调 Qwen-VL）/ pymysql（质检记录）/ opencv-python（相机采集）
- MySQL 8.x（本机 root，库 yunxiaoquan_qc 自动创建）
- NVIDIA GPU + 对应驱动（本机 RTX 3080）
- 依赖走阿里云镜像，paddle 走官方 cu126 源
- 外观质检需可访问 DashScope（阿里云百炼）北京区接口，密钥 `DASHSCOPE_API_KEY`
