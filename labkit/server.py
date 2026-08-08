"""chunk-lab 的本地 Web 服务。

只做三件事：把现有能力暴露成 HTTP 接口、托管单页前端、缓存最近一次评估结果。
不引入任何新依赖——Flask 来自 ragflow 的虚拟环境，前端是零构建的原生页面。

服务仅监听本机回环地址，是开发时工具，不面向网络暴露。
"""

import json  # 导入 json 解析查询串中的切分配置
import threading  # 导入 threading 用一把锁串行化耗时评估，避免并发重复计算
from pathlib import Path  # 导入 Path 定位前端静态文件

import markdown  # 导入 markdown 把评估报告渲染成 HTML 供页面预览
from flask import Flask, Response, jsonify, request, send_from_directory  # 导入 Flask 及其响应工具

from . import annotations, crawling, guard, runs  # 导入标注、爬取、回归护栏与运行历史模块
from .detect import detect  # 导入列表页结构自动识别
from .preview import (attach_source, attach_source_from_path, find_source,
                      render_chunk, viewable_pdf)  # 导入原文截图与格式转换能力
from .detectors import DetectorConfig  # 导入检测阈值配置
from .discover import scan_products  # 导入本机产物扫描
from .evaluate import (compare_reports, diff_chunk_texts, diff_findings, evaluate_all,
                       inspect_case, load_cases)  # 导入评估相关能力
from .ingest import ingest_from_path  # 导入按路径导入语料的能力
from .offline import DEFAULT_PARSER_CONFIG, build_parser_config  # 导入切分配置默认值与合并逻辑
from .parsing import (get_task, list_tasks, mineru_defaults, start_cloud_parse,
                      start_parse)  # 导入解析任务管理
from .paths import resolve_mineru_token  # 导入云端凭据读取
from .paths import CORPUS_DIR, DATA_ROOT, MINERU_OUT, REPORT_DIR, UPLOAD_DIR  # 导入目录常量
from .report import build_markdown  # 导入报告生成能力，用于按需重建缺失的报告

# 前端静态文件目录
WEB_DIR = Path(__file__).resolve().parent / "web"

# Flask 应用实例，静态资源由自定义路由处理故不启用默认静态目录
app = Flask(__name__, static_folder=None)

# 评估互斥锁：评估耗时十余秒且会占满 CPU，同一时刻只允许跑一轮
_eval_lock = threading.Lock()
# 最近一次评估结果缓存，供前端切换视图时免于重复计算
_last_report = None


# 可配置的切分参数定义，字段与生产知识库配置页逐项对应。
# name 取自 ragflow 的权威清单 api/utils/kb_runtime_config.py；
# stage 标明该参数作用于哪个阶段：chunk 影响切分（离线可调），
# parse 属解析阶段（已固化在缓存产物中，离线调整不会生效）。
CONFIG_FIELDS = [
    {"name": "parser_id", "label": "解析方法", "type": "select", "stage": "chunk",
     "options": ["naive", "book", "one", "paper", "manual", "laws", "presentation", "table", "picture"],
     "hint": "对应知识库的「解析方法」，General 即 naive。PPTX 在生产中固定走 presentation。"},
    {"name": "chunk_token_num", "label": "建议文本块大小", "type": "number", "stage": "chunk",
     "min": 64, "max": 4096, "step": 64, "hint": "单个切片的目标 token 上限。"},
    {"name": "delimiter", "label": "文本分段标识符", "type": "text", "stage": "chunk",
     "hint": "生产默认为 \\n。"},
    {"name": "enable_children", "label": "子块用于检索", "type": "bool", "stage": "chunk",
     "hint": "开启后按子分隔符再切一层，切分粒度变化极大。"},
    {"name": "children_delimiter", "label": "子分隔符", "type": "text", "stage": "chunk",
     "hint": "仅在「子块用于检索」开启时生效。"},
    {"name": "overlapped_percent", "label": "重叠百分比", "type": "number", "stage": "chunk",
     "min": 0, "max": 100, "step": 5, "hint": "相邻切片的重叠比例。"},
    {"name": "image_table_context_window", "label": "图像与表格上下文窗口", "type": "number",
     "stage": "chunk", "min": 0, "max": 10, "step": 1,
     "hint": "为图片与表格切片附带的上下文段落数。"},
    {"name": "toc_extraction", "label": "目录增强", "type": "bool", "stage": "chunk",
     "hint": "依赖对话模型，离线评估中不会生效。"},
    {"name": "html4excel", "label": "Excel 转 HTML", "type": "bool", "stage": "chunk",
     "hint": "影响 XLSX 的表格产出形态。"},
    {"name": "mineru_parse_method", "label": "MinerU 解析方法", "type": "select", "stage": "parse",
     "options": ["auto", "txt", "ocr"], "hint": "解析阶段参数，已固化在缓存产物中。"},
    {"name": "mineru_lang", "label": "MinerU 语言", "type": "select", "stage": "parse",
     "options": ["Chinese", "English"], "hint": "解析阶段参数，已固化在缓存产物中。"},
    {"name": "mineru_formula_enable", "label": "公式识别", "type": "bool", "stage": "parse",
     "hint": "解析阶段参数，已固化在缓存产物中。"},
    {"name": "mineru_table_enable", "label": "表格识别", "type": "bool", "stage": "parse",
     "hint": "解析阶段参数，已固化在缓存产物中。"},
]


def _config_from_request(payload):
    """从请求体解析本轮运行参数。

    切分配置整份透传给生产入口，不再逐字段挑拣——挑拣必然遗漏，
    此前就漏掉了 delimiter 与 overlapped_percent。
    """
    # 取出调用方给的切分配置；兼容旧版把参数平铺在请求体顶层的写法
    raw = payload.get("parser_config")
    # 未提供时从顶层收集已知字段，保证旧调用仍可工作
    if raw is None:
        raw = {f["name"]: payload[f["name"]] for f in CONFIG_FIELDS if f["name"] in payload}
    # 合并默认值并处理字段派生（enable_children 关闭时清空子分隔符等）
    config = build_parser_config(raw)
    # 检测阈值与切分预算取同一来源，避免超长判据与实际预算脱节
    cfg = DetectorConfig(chunk_token_num=int(config.get("chunk_token_num") or 512))
    # 返回检测配置与切分配置
    return cfg, config


@app.get("/")
def index():
    """返回单页前端。

    显式禁用缓存：这是开发时工具，前端改动频繁，浏览器缓存会让人对着
    旧页面反复怀疑功能没生效，排查成本远高于这点带宽。
    """
    # 取出静态文件响应
    resp = send_from_directory(WEB_DIR, "index.html")
    # 禁止任何层级的缓存，保证每次打开都是最新页面
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    # 兼容仅识别旧式头部的代理
    resp.headers["Pragma"] = "no-cache"
    # 明确标记为已过期
    resp.headers["Expires"] = "0"
    # 返回响应
    return resp


@app.get("/vendor/<path:name>")
def vendor(name):
    """提供随仓库自带的第三方前端库（pdf.js）。

    刻意不走 CDN：本工具常在没有外网的环境里用，CDN 挂掉会让整个预览页白屏。
    与生产 lzcj_web 用的是同一版本，渲染差异不会成为排查干扰项。
    """
    # 第三方库版本固定，可以放心让浏览器长期缓存
    resp = send_from_directory(WEB_DIR / "vendor", name)
    # 缓存一天，避免每次刷新都重传 1MB 的 worker
    resp.headers["Cache-Control"] = "public, max-age=86400"
    # 返回响应
    return resp


@app.get("/api/preview/<case_id>/file")
def api_preview_file(case_id):
    """返回样本关联的原始文件本身，供前端 pdf.js 直接加载。

    与 /shot 的分工：/shot 由服务端裁好单个切片的图，这里给的是整份原文，
    左侧全文视图要靠它才能翻页与缩放。
    """
    # 文件名由查询参数带入，与 /shot 保持一致
    filename = request.args.get("filename", "")
    # 定位已关联的原文
    src = find_source(case_id, filename)
    # 未关联时返回 404，前端据此提示去关联原文
    if src is None:
        return jsonify({"ok": False, "message": "该样本尚未关联原始文件"}), 404
    # Office 文档转成 PDF 后才能在浏览器里渲染；首次转换要几秒，之后走缓存
    pdf, why = viewable_pdf(src)
    # 转换不可用时把原因带出去，界面据此提示而不是显示空白框
    if pdf is None:
        return jsonify({"ok": False, "message": why}), 415
    # 按目录与文件名下发；conditional 让浏览器可用 If-Modified-Since 复用缓存
    return send_from_directory(pdf.parent, pdf.name, conditional=True)


@app.get("/api/config")
def api_config():
    """返回可配置字段的定义与默认值，供前端渲染配置面板。

    字段定义集中在服务端，前端只负责渲染，避免两边各维护一份而逐渐走样。
    """
    # 默认值取自与生产知识库对齐的配置；同时回报数据目录，
    # 数据已移到仓库外，界面上不显示的话使用者会找不到东西存在哪
    return jsonify({
        "fields": CONFIG_FIELDS,  # 字段定义
        "defaults": dict(DEFAULT_PARSER_CONFIG),  # 切分配置默认值
        "paths": {  # 数据落地位置，便于直接定位文件
            "data_root": str(DATA_ROOT),  # 数据根目录
            "corpus": str(CORPUS_DIR),  # 语料目录
            "mineru_out": str(MINERU_OUT),  # 解析产物目录
            "configurable_by": "环境变量 CHUNKLAB_DATA_DIR，或 chunk-lab/labconfig.json 的 data_dir",
        },
        # MinerU 连接与产物配置，字段与知识库的 MinerU 模型配置页一一对应
        "mineru": {
            "mineru_api": mineru_defaults()["mineru_api"],  # MinerU API 服务器
            "output_dir": str(MINERU_OUT),  # 输出目录路径
            "backend": mineru_defaults()["backend"],  # 处理后端类型
            "effort": mineru_defaults()["effort"],  # hybrid 系列的投入档位
            # 生产配置页的「处理完成后删除输出文件」默认开启，实验室固定关闭：
            # 离线重放依赖这些产物，删了就没法重放
            "delete_output": False,
        },
        # 当前被测代码的指纹。历史轮次都记了自己的指纹，界面拿这个一比，
        # 就能标出哪一轮的结果还对得上当前代码——改完代码没跑评估时，
        # 看着旧快照会误以为改动没生效
        "code_hash": runs.code_fingerprint()["hash"],
        # 云端解析可用性：界面据此决定是否显示云端入口，
        # 未配置 token 时给出配置指引而不是让人点了才报错
        "cloud": {
            "available": bool(resolve_mineru_token()),
            "backend": "vlm",
            "hint": "在 chunk-lab/labconfig.json 填 mineru_token，或设置环境变量 MINERU_API_TOKEN",
        },
    })


@app.get("/api/corpus")
def api_corpus():
    """列出语料库中的全部样本。"""
    # 加载语料描述
    cases = load_cases()
    # 路径对象不可直接序列化，转成字符串后返回
    return jsonify([
        {
            "case_id": c["case_id"],  # 样本标识
            "filename": c.get("filename", ""),  # 原始文件名
            "kind": c.get("kind", ""),  # 文档大类
            "block_count": c.get("block_count", 0),  # 原始块数
            "slide_mode": bool(c.get("slide_mode")),  # 是否按幻灯片切分
            "note": c.get("note", ""),  # 备注
        }
        for c in cases
    ])


@app.post("/api/parse")
def api_parse():
    """上传一个或多个文件并排队 MinerU 解析，立即返回任务标识。

    解析耗时数分钟，必须异步：同步等待会让请求超时、界面假死。
    多文件共用同一份表单参数（backend、解析方法等），每个文件一个独立任务，
    互不影响；任务在服务端串行执行，避免并发压垮本机 MinerU 服务。
    产物落在实验室自己的目录，与生产的 MinerU 输出彻底分开。
    """
    # 取出全部上传文件，过滤掉浏览器可能带来的空文件项
    files = [f for f in request.files.getlist("file") if f and f.filename]
    # 没有文件时明确提示
    if not files:
        return jsonify({"ok": False, "message": "未选择文件"}), 400
    # 确保上传目录存在
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # 解析方式：local 走本机 MinerU 服务，cloud 走官方精准解析 API
    mode = (request.form.get("mode") or "local").lower()
    # 云端缺 token 时提前拦下，避免文件都落盘了才失败
    if mode == "cloud" and not resolve_mineru_token():
        return jsonify({"ok": False,
                        "message": "未配置 MinerU token。请在 chunk-lab/labconfig.json "
                                   "填 mineru_token，或设置环境变量 MINERU_API_TOKEN"}), 400
    # 官方单批上限 200，超出时要求分批而不是截断
    if mode == "cloud" and len(files) > 200:
        return jsonify({"ok": False,
                        "message": f"官方单批最多 200 个文件，当前 {len(files)} 个，请分批提交"}), 400
    # 表单参数整批共用，循环外解析一次即可
    backend = request.form.get("backend") or "pipeline"  # 处理后端类型
    parse_method = request.form.get("parse_method") or "auto"  # 解析方法
    auto_import = request.form.get("auto_import", "1") != "0"  # 解析后是否自动入库
    kind = request.form.get("kind", "")  # 文档大类
    note = request.form.get("note", "")  # 备注
    mineru_api = request.form.get("mineru_api", "")  # MinerU API 服务器，留空用默认
    output_dir = request.form.get("output_dir", "")  # 产物输出目录，留空用实验室默认目录
    lang = request.form.get("lang") or "ch"  # 文档语言，仅云端使用

    # 本批已提交的文件名集合，用于跳过同名文件
    seen = set()
    # 已提交的任务列表，元素含任务标识与文件名
    tasks = []
    # 因同名被跳过的文件名列表
    skipped = []
    # 已落盘待整批提交的文件路径，仅云端使用
    saved = []
    # 逐个落盘并排队
    for file in files:
        # 保留原始文件名：切分器按扩展名分派类型，改名会导致走错分支
        name = Path(file.filename).name
        # 上传目录按文件名平铺存放，同名会互相覆盖，故一批内只收第一个
        if name in seen:
            # 记录被跳过的文件，如实回报而不是静默丢弃
            skipped.append(name)
            # 跳过后续处理
            continue
        # 记入已见文件名
        seen.add(name)
        # 目标路径
        dest = UPLOAD_DIR / name
        # 落盘
        file.save(dest)
        # 云端整批提交，此处只收集路径，排队动作放到循环外
        if mode == "cloud":
            # 记下落盘路径，供整批提交
            saved.append(dest)
            # 本文件处理完毕
            continue
        # 本地逐个排队解析，返回任务标识
        task_id = start_parse(
            dest,  # 已落盘的文件路径
            name,  # 原始文件名
            backend=backend,  # 处理后端类型
            parse_method=parse_method,  # 解析方法
            auto_import=auto_import,  # 解析后是否自动入库
            kind=kind,  # 文档大类
            note=note,  # 备注
            mineru_api=mineru_api,  # MinerU API 服务器
            output_dir=output_dir,  # 产物输出目录
        )
        # 记录本次提交结果
        tasks.append({"task_id": task_id, "filename": name})

    # 云端整批共用一个批次轮询，逐个提交既慢又浪费配额
    if mode == "cloud" and saved:
        # 整批提交，得到单个任务标识
        cloud_task = start_cloud_parse(
            saved,  # 本批全部文件路径
            auto_import=auto_import,  # 解析后是否自动入库
            kind=kind,  # 文档大类
            note=note,  # 备注
            lang=lang,  # 文档语言
        )
        # 批次内每个文件都指向同一个任务，便于界面统一展示
        tasks = [{"task_id": cloud_task, "filename": p.name} for p in saved]

    # 返回批量结果；task_id 保留单个值兼容只提交一个文件的旧调用方
    return jsonify({
        "ok": True,  # 提交成功
        "mode": mode,  # 实际使用的解析方式，便于界面区分提示
        "task_id": tasks[0]["task_id"] if tasks else None,  # 首个任务标识，兼容旧调用
        "task_ids": sorted({t["task_id"] for t in tasks}),  # 去重后的任务标识
        "tasks": tasks,  # 任务与文件名对照
        "count": len(tasks),  # 实际提交数量
        "skipped": skipped,  # 因同名被跳过的文件
    })


@app.post("/api/parse/cloud")
def api_parse_cloud():
    """把已下载的文件批量送到 MinerU 官方云端解析。

    本地一份 PDF 要十几分钟，云端快得多且一次可提交至多 200 个文件。
    """
    # 读取请求体
    payload = request.get_json(silent=True) or {}
    # 待解析文件
    paths = payload.get("paths") or []
    # 无选中项直接返回
    if not paths:
        return jsonify({"ok": False, "message": "没有选中文件"}), 400
    # 未配置 token 时提前拦下，避免提交后才失败
    if not resolve_mineru_token():
        return jsonify({"ok": False,
                        "message": "未配置 MinerU token。请在 chunk-lab/labconfig.json "
                                   "填 mineru_token，或设置环境变量 MINERU_API_TOKEN"}), 400
    # 限定在允许目录内，避免被诱导上传本机任意文件到云端
    try:
        allowed_dir = crawling.resolve_out_dir(payload.get("out_dir")).resolve()
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    # 逐个校验
    valid = []
    for raw in paths:
        p = Path(raw).resolve()
        # 越界或不存在的一律跳过
        try:
            p.relative_to(allowed_dir)
        except ValueError:
            continue
        if p.is_file():
            valid.append(p)
    # 没有合法文件时明确提示
    if not valid:
        return jsonify({"ok": False, "message": "选中的文件都不在允许的目录内"}), 400
    # 官方单批上限 200，超出时提示分批
    if len(valid) > 200:
        return jsonify({"ok": False,
                        "message": f"官方单批最多 200 个文件，当前 {len(valid)} 个，请分批提交"}), 400
    # 启动云端批量解析
    task_id = start_cloud_parse(valid, auto_import=True, note="来自网页抓取，云端解析")
    # 返回任务标识
    return jsonify({"ok": True, "task_id": task_id, "count": len(valid)})


@app.get("/api/parse/<task_id>")
def api_parse_status(task_id):
    """查询解析任务状态。"""
    # 读取任务
    task = get_task(task_id)
    # 任务不存在时返回 404；服务重启会清空任务表，这是预期行为
    if task is None:
        return jsonify({"ok": False, "message": "任务不存在（服务重启会清空任务列表）"}), 404
    # 返回任务状态
    return jsonify({"ok": True, "task": task})


@app.get("/api/parse")
def api_parse_list():
    """列出最近的解析任务。"""
    # 返回最近若干条
    return jsonify({"ok": True, "tasks": list_tasks()})


@app.get("/api/annotations/<case_id>")
def api_annotations(case_id):
    """读取某样本的人工标注。"""
    # 返回标注字典，键为切片序号
    return jsonify({"ok": True, "annotations": annotations.load(case_id)})


@app.post("/api/annotations/<case_id>")
def api_annotate(case_id):
    """写入一条人工标注：确认、误报，或标记检测器漏掉的问题。"""
    # 读取请求体
    payload = request.get_json(silent=True) or {}
    # 切片序号为必填
    if "chunk_index" not in payload:
        return jsonify({"ok": False, "message": "缺少 chunk_index"}), 400
    # 结论非法时明确报错而不是写入无法解释的数据
    try:
        item = annotations.save_one(
            case_id,  # 样本标识
            int(payload["chunk_index"]),  # 切片序号
            payload.get("verdict", ""),  # 标注结论
            detector=payload.get("detector", ""),  # 相关检测器或人工判定的问题类型
            note=payload.get("note", ""),  # 备注
            excerpt=payload.get("excerpt", ""),  # 正文摘要，用于日后校验序号是否错位
        )
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    # 返回写入的条目
    return jsonify({"ok": True, "annotation": item})


@app.post("/api/annotations/<case_id>/batch")
def api_annotate_batch(case_id):
    """整类批量写入标注：把某条规则命中的全部切片一次判定完。

    一条规则命中上百个切片时逐条判定不现实，因此提供整批入口；
    已标注的切片默认跳过，不覆盖此前逐条给出的判断。
    """
    # 读取请求体
    payload = request.get_json(silent=True) or {}
    # 待标注的切片清单为必填
    items = payload.get("items")
    # 清单必须是列表，否则无法逐条处理
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "message": "缺少 items"}), 400
    # 结论非法时明确报错而不是写入无法解释的数据
    try:
        result = annotations.save_many(
            case_id,  # 样本标识
            items,  # 切片清单，每项含序号与正文摘要
            payload.get("verdict", ""),  # 整批统一的标注结论
            detector=payload.get("detector", ""),  # 当前筛选的问题类型
            note=payload.get("note", ""),  # 整批共用的备注
            # 是否覆盖已有标注，默认跳过
            skip_annotated=not payload.get("overwrite", False),
        )
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    # 返回写入与跳过的数量
    return jsonify({"ok": True, **result})


@app.post("/api/annotations/<case_id>/batch/delete")
def api_unannotate_batch(case_id):
    """整类批量撤销标注。

    用 POST 而非 DELETE：批量撤销需要在请求体里带上切片序号清单，
    而 DELETE 携带请求体在各类客户端与代理上的支持并不一致。
    """
    # 读取请求体
    payload = request.get_json(silent=True) or {}
    # 待撤销的切片序号清单为必填
    indexes = payload.get("chunk_indexes")
    # 清单必须是列表，否则无法逐个删除
    if not isinstance(indexes, list) or not indexes:
        return jsonify({"ok": False, "message": "缺少 chunk_indexes"}), 400
    # 执行批量删除
    removed = annotations.delete_many(case_id, indexes)
    # 返回实际删除条数
    return jsonify({"ok": True, "removed": removed})


@app.get("/api/annotations/<case_id>/regions")
def api_regions(case_id):
    """读取某样本在原文上圈出的全部异常区域。"""
    # 直接返回列表
    return jsonify(annotations.load_regions(case_id))


@app.post("/api/annotations/<case_id>/regions")
def api_add_region(case_id):
    """记录一块人工圈出的异常区域。

    与切片标注的分工：切片标注是对切分器已产出的块做判定，这里记的是
    切分器压根没在那儿切出块来的地方——正是检测规则该补的方向。
    """
    # 读取请求体
    payload = request.get_json(silent=True) or {}
    # 坐标非法时明确报错而不是写入无法还原的数据
    try:
        item = annotations.save_region(
            case_id,  # 样本标识
            payload.get("region") or [],  # 区域坐标
            detector=payload.get("detector", ""),  # 人工判定的问题类型
            note=payload.get("note", ""),  # 备注
            # 记下当前代码指纹：区域问题无法自动核对，只能标明发现于哪个版本
            code_hash=runs.code_fingerprint()["hash"],
        )
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    # 返回写入的条目
    return jsonify({"ok": True, "region": item})


@app.delete("/api/annotations/<case_id>/regions/<region_id>")
def api_del_region(case_id, region_id):
    """删除一条区域标注。"""
    # 执行删除并回报是否命中
    ok = annotations.delete_region(case_id, region_id)
    # 未命中时返回 404，便于界面区分「删掉了」与「本来就没有」
    return (jsonify({"ok": True}), 200) if ok else (jsonify({"ok": False, "message": "没有这条标注"}), 404)


@app.delete("/api/annotations/<case_id>/<int:chunk_index>")
def api_unannotate(case_id, chunk_index):
    """删除一条标注。"""
    # 执行删除
    annotations.delete_one(case_id, chunk_index)
    # 返回成功
    return jsonify({"ok": True})


@app.get("/api/annotations")
def api_annotation_stats():
    """规则质量统计：按指定版本核对后的准确率与待办。

    统计基于核对结果而非直接数标注文件——标注是跨版本累积的，
    修好的误报若照样计入准确率，就等于用旧账压现在的成绩。
    run 指定版本，为空表示用当前代码实时切分核对。
    """
    # 可按样本收窄
    only = request.args.get("case")
    # 切分参数与预览页一致，否则切片边界不同、标注对不上
    raw = request.args.get("parser_config")
    # 解析失败时用默认配置
    try:
        config = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        config = None
    # 执行统计
    return jsonify({"ok": True, "stats": guard.stats(
        run_id=request.args.get("run") or None,  # 目标版本
        only=[only] if only else None,  # 可只看某个样本
        parser_config=config,  # 切分参数
    )})


@app.get("/api/guard")
def api_guard():
    """按当前代码重跑一遍，核对人工标注是否仍然成立。

    这是改检测规则时的护栏：没有它，修好一条误报的同时碰坏另一条，
    要等下一轮全量评估才发现，甚至根本发现不了。
    """
    # 可按样本收窄，便于针对一个文档快速迭代
    only = request.args.get("case")
    # 切分参数与预览页保持一致，否则切片边界不同、标注对不上
    raw = request.args.get("parser_config")
    # 解析失败时用默认配置，不因参数格式问题让护栏跑不起来
    try:
        config = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        config = None
    # 执行核对；run 指定版本，为空表示用当前代码实时切分
    return jsonify(guard.check(only=[only] if only else None, parser_config=config,
                               run_id=request.args.get("run") or None))


@app.get("/api/guard/false-positives")
def api_false_positives():
    """归纳误报的共同特征，指出规则该往哪儿改。

    逐条看误报很难看出规律；把同一条规则的反例放在一起，
    命中的是什么、出现在什么格式的文档里，往往一眼就能看出来。
    """
    # 版本一并带上：不按版本过滤会出现「仍在误报 17 条、共性却写 24 条」的矛盾
    only = request.args.get("case")
    # 切分参数与统计保持一致
    raw = request.args.get("parser_config")
    # 解析失败时用默认配置
    try:
        config = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        config = None
    # 归纳
    return jsonify({
        "ok": True,
        "groups": guard.analyze_false_positives(
            detector=request.args.get("detector"),  # 只看某一条规则
            only=[only] if only else None,  # 只看某个样本
            run_id=request.args.get("run") or "",  # 目标版本；空串表示当前代码
            parser_config=config,  # 切分参数
        ),
    })


@app.get("/api/guard/marks")
def api_marks():
    """按结论列出标注明细，供界面从统计数字点进去落到具体切片。"""
    # 结论必须指定，否则不知道要列哪一类
    verdict = request.args.get("verdict", "")
    # 非法结论明确报错而不是返回空列表
    if verdict not in annotations.VERDICTS:
        return jsonify({"ok": False, "message": f"未知的标注结论：{verdict}"}), 400
    # 切分参数与统计保持一致
    raw = request.args.get("parser_config")
    # 解析失败时用默认配置
    try:
        config = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        config = None
    # 可按样本收窄
    only = request.args.get("case")
    # 返回明细；按版本过滤后条数与统计数字一致
    return jsonify({
        "ok": True,
        "items": guard.list_marks(
            verdict,  # 结论
            detector=request.args.get("detector"),  # 只看某一类问题
            only=[only] if only else None,  # 只看某个样本
            run_id=request.args.get("run") or "",  # 目标版本；空串表示当前代码
            parser_config=config,  # 切分参数
        ),
    })


@app.get("/api/guard/regions")
def api_all_regions():
    """汇总全部样本在原文上圈出的异常区域。

    区域标注按样本分文件存，但看的时候需要横着看：哪些文档被圈得最多、
    人反复圈出的是同一类什么问题，跨样本才看得出来。
    """
    # 汇总并附上文件名，界面据此才能加载原文定位
    items = annotations.all_regions()
    # 建立样本到文件名的映射，避免逐条去查
    names = {c["case_id"]: c.get("filename", "") for c in load_cases()}
    # 补上文件名后返回
    return jsonify({
        "ok": True,
        "items": [{**it, "filename": names.get(it["case_id"], "")} for it in items],
    })


@app.get("/api/guard/report")
def api_full_report():
    """生成一份完整的优化任务书，可直接交给模型动手改。

    与单条规则报告的区别是「完整」：把当前全部待办放在一起，
    并写清代码位置、注册方式与验证命令，拿到的人不必回头再问。
    """
    # 切分参数与预览页保持一致，否则证据对不上
    raw = request.args.get("parser_config")
    # 解析失败时用默认配置
    try:
        config = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        config = None
    # 生成报告；只列在目标版本下仍然成立的问题
    r = guard.build_full_report(parser_config=config,
                                run_id=request.args.get("run") or None)
    # 需要下载时以附件形式返回，浏览器直接存成文件
    if request.args.get("download") == "1":
        # 文件名带日期，多次生成不会互相覆盖
        from datetime import datetime
        name = f"切分规则优化任务_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
        # 用 Response 直出，避免再写一次临时文件
        # mimetype 会自行补上 charset，此处不要再手写，否则响应头里会出现两次
        resp = Response(r["markdown"], mimetype="text/markdown")
        # RFC 5987 编码文件名，中文名才不会乱码
        from urllib.parse import quote
        resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
        return resp
    # 默认返回 JSON，供页面预览
    return jsonify(r)


@app.get("/api/guard/report/<detector>")
def api_rule_report(detector):
    """为某条规则生成优化报告。

    报告带上规则的当前实现与反例此刻的真实命中证据，
    拿到就能直接动手改，不必再回头翻代码和原文。
    """
    # 切分参数与预览页保持一致，否则证据对不上
    raw = request.args.get("parser_config")
    # 解析失败时用默认配置
    try:
        config = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        config = None
    # 生成报告
    r = guard.build_optimization_report(
        detector,  # 规则名
        only=[request.args.get("case")] if request.args.get("case") else None,  # 可只看某个样本
        parser_config=config,  # 切分参数
        run_id=request.args.get("run") or "",  # 目标版本；空串表示当前代码
    )
    # 无反例时返回 404，界面据此提示先去标注
    return (jsonify(r), 200) if r.get("ok") else (jsonify(r), 404)


@app.post("/api/preview/<case_id>/source")
def api_attach_source(case_id):
    """为样本关联原始文件，之后才能渲染切片截图。"""
    # 优先接收上传的文件
    file = request.files.get("file")
    # 其次接受本机路径，避免大文件重复上传
    local = (request.form.get("path") or "").strip()
    # 处理上传
    if file is not None and file.filename:
        dest = attach_source(case_id, Path(file.filename).name, file)
    elif local:
        # 本机路径必须存在
        src = Path(local).expanduser()
        if not src.is_file():
            return jsonify({"ok": False, "message": f"文件不存在：{src}"}), 400
        dest = attach_source_from_path(case_id, src.name, src)
    else:
        return jsonify({"ok": False, "message": "请上传文件或提供本机路径"}), 400
    # 返回关联结果
    return jsonify({"ok": True, "path": str(dest)})


@app.post("/api/preview/<case_id>/shot")
def api_chunk_shot(case_id):
    """按切片坐标渲染原文截图。"""
    # 读取请求体
    payload = request.get_json(silent=True) or {}
    # 渲染并返回；各类前置条件不满足时以 ok=False 携带原因返回
    return jsonify(render_chunk(
        case_id,  # 样本标识
        payload.get("filename", ""),  # 原始文件名
        payload.get("positions") or [],  # 切片坐标
    ))


@app.post("/api/crawl")
def api_crawl():
    """启动一次列表页抓取，立即返回任务标识。

    抓取耗时不可预期，必须异步；参数与命令行 ./run.sh crawl 完全一致，
    两边共用同一个 Crawler 实现。
    """
    # 读取请求体
    payload = request.get_json(silent=True) or {}
    # 起始页为必填
    url = (payload.get("url") or "").strip()
    # 缺少 URL 时明确提示
    if not url:
        return jsonify({"ok": False, "message": "请填写起始列表页 URL"}), 400
    # 只接受 http(s)，避免被诱导去读本地文件
    if not url.startswith(("http://", "https://")):
        return jsonify({"ok": False, "message": "仅支持 http/https 地址"}), 400
    # 校验下载目录；非法路径要在启动前拦下，而不是让后台线程失败
    try:
        out_dir = crawling.resolve_out_dir(payload.get("out_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    # 启动后台抓取
    task_id = crawling.start_crawl(
        url,  # 起始列表页
        out_dir=out_dir,  # 下载目录，可由界面指定
        exts=payload.get("exts") or "pdf,doc,docx,ppt,pptx,xls,xlsx",  # 目标扩展名
        next_text=payload.get("next_text", ""),  # 翻页文字
        next_selector=payload.get("next_selector", ""),  # 翻页选择器
        detail_selector=payload.get("detail_selector", ""),  # 详情页选择器
        max_pages=int(payload.get("max_pages") or 10),  # 翻页上限
        max_files=int(payload.get("max_files") or 0),  # 文件数上限
        delay=float(payload.get("delay") or 1.0),  # 全局最小请求间隔
        workers=int(payload.get("workers") or 4),  # 并发下载数
        link_pattern=payload.get("link_pattern", ""),  # 下载链接正则
        json_field=payload.get("json_field", ""),  # JSON 中的文件路径字段
        url_prefix=payload.get("url_prefix", ""),  # 相对路径的拼接前缀
        page_param=payload.get("page_param", ""),  # JSON 接口翻页参数名
        obey_robots=payload.get("obey_robots", True),  # 是否遵守 robots.txt
        dry_run=bool(payload.get("dry_run")),  # 是否只演练
    )
    # 返回任务标识供前端轮询
    return jsonify({"ok": True, "task_id": task_id})


@app.post("/api/crawl/detect")
def api_crawl_detect():
    """探测列表页结构，返回可直接填入表单的配置。

    手工填字段路径与地址前缀很折磨，而这些都能从响应本身推断出来。
    """
    # 读取请求体
    payload = request.get_json(silent=True) or {}
    # 起始页为必填
    url = (payload.get("url") or "").strip()
    # 缺少 URL 时明确提示
    if not url:
        return jsonify({"ok": False, "message": "请先填写列表页地址"}), 400
    # 只接受 http(s)
    if not url.startswith(("http://", "https://")):
        return jsonify({"ok": False, "message": "仅支持 http/https 地址"}), 400
    # 执行探测；内部已捕获网络异常并转为可读信息
    return jsonify(detect(url))


@app.get("/api/crawl")
def api_crawl_list():
    """列出最近的抓取任务与已下载文件。"""
    # 可按目录查看，下载目录允许在界面上更改
    try:
        out_dir = crawling.resolve_out_dir(request.args.get("dir"))
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    # 任务与文件一并返回，前端一次请求即可刷新整页
    return jsonify({"ok": True, "tasks": crawling.list_tasks(),
                    "files": crawling.list_downloads(out_dir=out_dir),
                    "default_dir": str(crawling.DOWNLOAD_DIR),
                    "current_dir": str(out_dir)})


@app.get("/api/crawl/<task_id>")
def api_crawl_status(task_id):
    """查询抓取任务状态。"""
    # 读取任务
    task = crawling.get_task(task_id)
    # 不存在时返回 404；服务重启会清空任务表
    if task is None:
        return jsonify({"ok": False, "message": "任务不存在（服务重启会清空任务列表）"}), 404
    # 返回状态
    return jsonify({"ok": True, "task": task})


@app.post("/api/crawl/parse")
def api_crawl_parse():
    """把已下载的文件送去 MinerU 解析并入库。"""
    # 读取请求体
    payload = request.get_json(silent=True) or {}
    # 待解析的文件路径列表
    paths = payload.get("paths") or []
    # 无选中项时直接返回
    if not paths:
        return jsonify({"ok": False, "message": "没有选中文件"}), 400
    # 解析范围限定在下载目录内，避免被诱导解析本机任意文件；
    # 下载目录可在界面上更改，故以本次请求指定的目录为准
    try:
        allowed_dir = crawling.resolve_out_dir(payload.get("out_dir")).resolve()
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    # 逐个提交解析任务；解析本身是异步的，这里只负责排队
    task_ids = []
    # 遍历选中文件
    for raw in paths:
        # 规范化后校验是否在允许目录内
        p = Path(raw).resolve()
        # 越界路径直接跳过
        try:
            p.relative_to(allowed_dir)
        except ValueError:
            continue
        # 文件必须存在
        if not p.is_file():
            continue
        # 提交解析
        task_ids.append(start_parse(
            p,  # 文件路径
            p.name,  # 原始文件名
            backend=payload.get("backend") or "pipeline",  # 处理后端类型
            parse_method=payload.get("parse_method") or "auto",  # 解析方法
            auto_import=True,  # 解析完直接入库，省去再走一遍导入
            note="来自网页抓取",  # 备注来源
        ))
    # 返回已提交的任务
    return jsonify({"ok": True, "task_ids": task_ids, "count": len(task_ids)})


@app.get("/api/sources")
def api_sources():
    """扫描本机 MinerU 产物，返回可导入的候选列表。"""
    # 扫描并按文档名与 backend 去重
    return jsonify(scan_products())


@app.post("/api/corpus/import")
def api_import():
    """把选中的产物导入语料库。"""
    # 读取请求体
    payload = request.get_json(silent=True) or {}
    # 待导入项列表
    items = payload.get("items") or []
    # 无选中项时直接返回，避免无谓处理
    if not items:
        return jsonify({"ok": False, "message": "没有选中任何产物"}), 400
    # 收集逐项结果
    results = []
    # 逐个导入
    for item in items:
        # 单项失败不应中断整批导入
        try:
            # 执行导入
            case_id, status = ingest_from_path(
                item["path"],  # 产物路径
                item["filename"],  # 原始文件名
                kind=item.get("kind", ""),  # 文档大类
                slide=bool(item.get("slide")),  # 是否按幻灯片切分
                overwrite=bool(payload.get("overwrite")),  # 是否覆盖同名样本
            )
            # 记录成功结果
            results.append({"case_id": case_id, "status": status, "ok": True})
        except Exception as e:
            # 记录失败原因
            results.append({"case_id": item.get("filename", "?"), "status": f"{type(e).__name__}: {e}", "ok": False})
    # 返回全部结果
    return jsonify({"ok": True, "results": results})


def _strip_findings(report):
    """剥离每个样本的 findings 明细。

    概览与对比只用逐样本统计，明细在切片预览里按需加载；
    带上会让响应体积膨胀数十倍而没有对应收益。
    """
    # 浅拷贝后替换 cases，避免污染磁盘上的快照数据
    out = dict(report)
    # 逐样本去掉 findings 字段
    out["cases"] = [{k: v for k, v in c.items() if k != "findings"} for c in report.get("cases", [])]
    # 返回精简后的报告
    return out


@app.post("/api/eval")
def api_eval():
    """跑一轮全量评估，存为新的历史轮次，并可与任意历史轮次对比。"""
    # 声明使用模块级缓存
    global _last_report
    # 读取请求体
    payload = request.get_json(silent=True) or {}
    # 解析运行参数：检测阈值 + 完整切分配置
    cfg, config = _config_from_request(payload)
    # 限定评估的样本，为空表示全量
    only = payload.get("cases") or None
    # 上一轮尚未结束时拒绝新请求，避免 CPU 争抢导致两轮都变慢
    if not _eval_lock.acquire(blocking=False):
        return jsonify({"ok": False, "message": "已有评估正在运行，请稍候"}), 409
    # 确保无论成败都释放锁
    try:
        # 执行评估
        report = evaluate_all(only=only, cfg=cfg, parser_config=config)
        # 存为不可变的历史轮次快照，返回其标识
        run_id = runs.save_run(report, label=payload.get("label", ""))
        # 重新读出快照，使响应带上代码指纹等落盘时补充的字段
        report = runs.load_run(run_id)
        # 缓存结果供其它视图复用
        _last_report = report
        # 组装响应，剥离明细以控制体积
        result = {"ok": True, "run_id": run_id, "report": _strip_findings(report)}
        # 按需与指定轮次对比；compare 可以是 baseline / latest / 具体 run_id
        ref = payload.get("compare")
        # 仅在明确要求时才做对比
        if ref:
            # 解析对比目标；本轮刚存入历史，latest 会指向自己，故对比目标取上一轮
            target = runs.resolve_run(ref)
            # 目标存在且不是本轮自己时才计算升降
            if target and target.get("run_id") != run_id:
                # 附上对比结果
                result["comparison"] = compare_reports(target, report)
            else:
                # 明确告知无可对比的轮次，前端据此提示
                result["comparison"] = None
        # 按需把本轮设为新的对比基准
        if payload.get("set_baseline"):
            # 基线只是指针，指向本轮即可
            runs.set_baseline(run_id)
            # 标记已更新
            result["baseline_updated"] = True
        # 返回结果
        return jsonify(result)
    finally:
        # 释放评估锁
        _eval_lock.release()


@app.get("/api/runs")
def api_runs():
    """返回全部历史轮次摘要，并标出当前基准。"""
    # 读取索引
    index = runs.list_runs()
    # 当前基准的轮次标识
    baseline_id = runs.get_baseline_id()
    # 逐条标注是否为基准，前端据此高亮
    for item in index:
        item["is_baseline"] = item.get("run_id") == baseline_id
    # 返回列表与基准标识
    return jsonify({"runs": index, "baseline_id": baseline_id})


@app.get("/api/runs/<run_id>")
def api_run_detail(run_id):
    """返回某一轮的完整结果，供概览页渲染。"""
    # 解析轮次引用，支持 baseline / latest / 具体标识
    report = runs.resolve_run(run_id)
    # 不存在时返回 404
    if report is None:
        return jsonify({"ok": False, "message": f"轮次不存在：{run_id}"}), 404
    # 剥离明细后返回
    return jsonify(_strip_findings(report))


@app.post("/api/runs/<run_id>/label")
def api_set_label(run_id):
    """给某一轮打备注，便于日后辨认这一轮做了什么改动。"""
    # 读取请求体
    payload = request.get_json(silent=True) or {}
    # 更新备注
    ok = runs.set_label(run_id, payload.get("label", ""))
    # 轮次不存在时返回 404
    if not ok:
        return jsonify({"ok": False, "message": "轮次不存在"}), 404
    # 返回成功
    return jsonify({"ok": True})


@app.delete("/api/runs/<run_id>")
def api_delete_run(run_id):
    """删除某一轮及其全部衍生数据：快照、切分文本、报告、索引记录。"""
    # 执行删除；若它是当前基准，指针会被一并清空
    result = runs.delete_run(run_id)
    # 如实回报删了哪些文件，而不是笼统说一句已删除
    return jsonify(result)


@app.get("/api/compare")
def api_compare():
    """对比任意两轮。参数 a 为基准、b 为对比对象，均支持 baseline / latest / run_id。"""
    # 解析基准轮次
    a = runs.resolve_run(request.args.get("a") or "baseline")
    # 解析对比对象
    b = runs.resolve_run(request.args.get("b") or "latest")
    # 任一不存在都无法对比
    if a is None or b is None:
        return jsonify({"ok": False, "message": "指定的轮次不存在"}), 404
    # 同一轮次自比没有意义，明确拒绝
    if a.get("run_id") == b.get("run_id"):
        return jsonify({"ok": False, "message": "两侧是同一轮次，无法对比"}), 400
    # 返回对比结果
    return jsonify({"ok": True, "comparison": compare_reports(a, b)})


@app.get("/api/diff")
def api_diff():
    """下钻两轮之间的具体差异：哪些问题是新增的，哪些消失了。

    对比只给出「+6」这样的变化量，无法据此修复；本接口回答「是哪 6 条」。
    """
    # 解析基准轮次
    a = runs.resolve_run(request.args.get("a") or "baseline")
    # 解析对比对象
    b = runs.resolve_run(request.args.get("b") or "latest")
    # 任一不存在都无法比较
    if a is None or b is None:
        return jsonify({"ok": False, "message": "指定的轮次不存在"}), 404
    # 可按检测器与样本进一步收窄范围
    detector = request.args.get("detector") or None
    # 样本筛选
    case_id = request.args.get("case") or None
    # 计算差异并返回
    return jsonify({"ok": True, "diff": diff_findings(a, b, detector=detector, case_id=case_id)})


@app.get("/api/runs/<run_id>/chunks/<case_id>")
def api_run_chunks(run_id, case_id):
    """读取某一轮存下来的切分文本。

    与实时切分的区别至关重要：实时切分用的是当前代码，代码一改就再也
    得不到旧结果；这里返回的是那一轮当时真实的切分内容。
    """
    # 解析轮次引用，支持 baseline / latest / 具体标识
    report = runs.resolve_run(run_id)
    # 轮次不存在时返回 404
    if report is None:
        return jsonify({"ok": False, "message": f"轮次不存在：{run_id}"}), 404
    # 读取该轮该样本的切分文本
    chunks = runs.load_chunks(report.get("run_id", run_id), case_id)
    # 旧轮次没有文本快照，明确告知而不是返回空数组造成误解
    if chunks is None:
        return jsonify({"ok": False, "message": "该轮次没有保存切分文本（在此功能之前产生）"}), 404
    # 返回该轮的切分文本与当时的参数
    return jsonify({
        "ok": True,  # 请求成功
        "run_id": report.get("run_id", run_id),  # 轮次标识
        "case_id": case_id,  # 样本标识
        "config": report.get("config", {}),  # 该轮的切分参数
        "label": report.get("label", ""),  # 该轮备注
        "chunks": chunks,  # 切分文本
    })


@app.get("/api/runs/<run_id>/report")
def api_run_report(run_id):
    """返回某一轮的 Markdown 评估报告。

    format=html 时服务端渲染为 HTML 供页面预览，
    download=1 时作为附件下载，其余情况返回 Markdown 原文。
    """
    # 解析轮次
    report = runs.resolve_run(run_id)
    # 轮次不存在时返回 404
    if report is None:
        return jsonify({"ok": False, "message": f"轮次不存在：{run_id}"}), 404
    # 真实轮次标识，用于定位报告文件
    rid = report.get("run_id", run_id)
    # 报告文件路径
    path = REPORT_DIR / f"{rid}.md"
    # 报告文件可能被清理或该轮产生于报告功能之前，此时按轮次数据即时重建，
    # 保证任何一轮都能拿到报告，而不是给用户一个死链
    if path.is_file():
        md = path.read_text(encoding="utf-8")
    else:
        # 从文本快照取切片全文用于完整案例
        md = build_markdown(report, chunks_by_case=runs.load_chunks(rid))
        # 顺手落盘，下次直接读取
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(md, encoding="utf-8")

    # 下载模式：作为附件返回，文件名带轮次标识便于归档
    if request.args.get("download"):
        # 构造附件响应；mimetype 只给类型，charset 由 Flask 自动补上，
        # 手写 charset 会与之重复出现在 Content-Type 里
        resp = Response(md, mimetype="text/markdown")
        # 指定下载文件名
        resp.headers["Content-Disposition"] = f'attachment; filename="chunk-lab-{rid}.md"'
        return resp

    # 预览模式：服务端渲染为 HTML，前端直接嵌入展示
    if request.args.get("format") == "html":
        # 启用表格、代码块与目录扩展，报告大量使用表格与围栏代码块
        html = markdown.markdown(md, extensions=["tables", "fenced_code", "toc", "sane_lists"])
        # 以 JSON 返回，便于前端连同元信息一起处理
        return jsonify({"ok": True, "run_id": rid, "html": html})

    # 默认返回 Markdown 原文，用纯文本类型以便浏览器直接展示而非下载
    return Response(md, mimetype="text/plain")


@app.get("/api/chunkdiff")
def api_chunk_diff():
    """对比两轮在某个样本上的切分文本差异。

    回答的是「这一段的边界从哪挪到了哪」，而不只是「问题数变了多少」。
    """
    # 解析两侧轮次
    a = runs.resolve_run(request.args.get("a") or "baseline")
    b = runs.resolve_run(request.args.get("b") or "latest")
    # 样本标识为必填
    case_id = request.args.get("case")
    # 缺少样本时无法比较
    if not case_id:
        return jsonify({"ok": False, "message": "缺少 case 参数"}), 400
    # 任一轮次不存在都无法比较
    if a is None or b is None:
        return jsonify({"ok": False, "message": "指定的轮次不存在"}), 404
    # 分别读取两轮的切分文本
    ca = runs.load_chunks(a.get("run_id"), case_id)
    cb = runs.load_chunks(b.get("run_id"), case_id)
    # 任一侧缺快照时明确告知是哪一轮缺
    if ca is None or cb is None:
        missing = a.get("run_id") if ca is None else b.get("run_id")
        return jsonify({"ok": False, "message": f"轮次 {missing} 没有保存切分文本，无法做文本对比"}), 404
    # 计算文本差异
    result = diff_chunk_texts(ca, cb)
    # 附上两侧的轮次信息，便于前端标注比的是什么
    return jsonify({
        "ok": True,  # 请求成功
        "case_id": case_id,  # 样本标识
        "a": {"run_id": a.get("run_id"), "label": a.get("label", ""), "config": a.get("config", {})},  # 基准轮信息
        "b": {"run_id": b.get("run_id"), "label": b.get("label", ""), "config": b.get("config", {})},  # 对比轮信息
        "diff": result,  # 文本差异明细
    })


@app.post("/api/baseline")
def api_set_baseline():
    """把指定轮次设为对比基准；未指定时使用最近一次评估结果。"""
    # 读取请求体
    payload = request.get_json(silent=True) or {}
    # 优先使用显式指定的轮次
    run_id = payload.get("run_id")
    # 未指定时回落到最近一次评估
    if not run_id:
        # 尚未跑过评估时无从设定
        if _last_report is None:
            return jsonify({"ok": False, "message": "请先运行一次评估，或指定一个历史轮次"}), 400
        # 取最近一轮的标识
        run_id = _last_report.get("run_id")
    # 目标轮次必须存在
    if runs.load_run(run_id) is None:
        return jsonify({"ok": False, "message": f"轮次不存在：{run_id}"}), 404
    # 设定基准指针
    runs.set_baseline(run_id)
    # 返回成功
    return jsonify({"ok": True, "run_id": run_id})


@app.get("/api/baseline")
def api_get_baseline():
    """返回当前基准轮次的完整结果，供页面打开时直接渲染。"""
    # 解析基准指向的轮次
    baseline = runs.resolve_run("baseline")
    # 未设定基准或指向的轮次已被删除时，回落到最新一轮，使页面仍有内容可看
    if baseline is None:
        # 尝试取最新一轮
        baseline = runs.resolve_run("latest")
        # 确实没有任何历史时返回空
        if baseline is None:
            return jsonify({"exists": False})
        # 标记这不是真正的基准，只是回落展示
        data = _strip_findings(baseline)
        data.update({"exists": True, "is_baseline": False})
        return jsonify(data)
    # 返回基准轮次的完整结果
    data = _strip_findings(baseline)
    data.update({"exists": True, "is_baseline": True})
    return jsonify(data)


@app.get("/api/case/<case_id>")
def api_case_detail(case_id):
    """返回单个样本的全部切片内容与问题标注，用于预览切分效果。"""
    # 只加载目标样本
    cases = load_cases(only=[case_id])
    # 样本不存在时返回 404
    if not cases:
        return jsonify({"ok": False, "message": f"样本不存在：{case_id}"}), 404
    # 预览与评估必须同口径，否则切片序号对不上。
    # 配置从查询串取，值为 JSON 以便传递布尔与数字而不丢类型。
    raw = request.args.get("parser_config")
    # 解析失败时回落到默认配置，不因参数格式问题让预览整体不可用
    try:
        overrides = json.loads(raw) if raw else {}
    except ValueError:
        overrides = {}
    # 合并默认值
    config = build_parser_config(overrides)
    # 检测阈值与切分预算同源
    cfg = DetectorConfig(chunk_token_num=int(config.get("chunk_token_num") or 512))
    # with_positions 时渲染页面图像以获得真实坐标，代价是慢一些，
    # 故由前端在需要截图时才请求
    want_pos = request.args.get("positions") == "1"
    # 生成预览数据
    data = inspect_case(cases[0], cfg=cfg, parser_config=config, with_positions=want_pos)
    # 附带该样本是否已关联原始文件，界面据此决定是否显示截图入口
    src = find_source(cases[0]["case_id"], cases[0].get("filename", ""))
    data["source_file"] = str(src) if src else None
    # 附带人工标注，前端在切片上直接展示
    data["annotations"] = annotations.load(cases[0]["case_id"])
    return jsonify(data)


def serve(host="127.0.0.1", port=5099, debug=False, reload=True):
    """启动本地服务。默认只监听回环地址，不对外暴露。

    reload 打开后改代码即时生效，不必手动重启。代价是 Flask 的重载器会
    再起一个子进程，模块加载一次约十几秒；关掉它可以省下这份开销。
    """
    # 启动时把旧版单文件基线收编为首个历史轮次，保证升级前后基准不断档
    migrated = runs.migrate_legacy_baseline()
    # 确有迁移时提示，便于使用者知道历史从何而来
    if migrated:
        print(f"已把旧版基线迁移为历史轮次：{migrated}")
    # 提示访问地址，便于直接点击打开
    print(f"chunk-lab 控制台：http://{host}:{port}")
    # 明确告知热加载状态，避免改了代码不生效却不知道原因
    print(f"热加载：{'已开启，改 labkit/ 下的代码会自动重启' if reload else '已关闭'}")
    # 只监视本项目代码。默认重载器会盯着 sys.modules 里的全部文件，
    # 那包含整个 ragflow 与站点包，动辄上万个文件，既慢又会因无关变动误触发。
    extra_files = None
    # 开启热加载时收集需要监视的文件清单
    if reload:
        # 前端页面改动也应触发重启，否则要手动刷新缓存才生效
        extra_files = [str(p) for p in (WEB_DIR).rglob("*.html")]
    # 启动 Flask 开发服务器
    app.run(host=host, port=port, debug=debug,
            use_reloader=reload,  # 热加载开关
            reloader_type="stat",  # 用轮询而非 watchdog，避免额外依赖
            extra_files=extra_files)  # 额外监视前端文件
