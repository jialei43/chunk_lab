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

from . import runs  # 导入运行历史模块，负责轮次快照与基准指针
from .detectors import DetectorConfig  # 导入检测阈值配置
from .discover import scan_products  # 导入本机产物扫描
from .evaluate import (compare_reports, diff_chunk_texts, diff_findings, evaluate_all,
                       inspect_case, load_cases)  # 导入评估相关能力
from .ingest import ingest_from_path  # 导入按路径导入语料的能力
from .offline import DEFAULT_PARSER_CONFIG, build_parser_config  # 导入切分配置默认值与合并逻辑
from .parsing import UPLOAD_DIR, get_task, list_tasks, start_parse  # 导入解析任务管理
from .paths import REPORT_DIR  # 导入报告目录常量
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


@app.get("/api/config")
def api_config():
    """返回可配置字段的定义与默认值，供前端渲染配置面板。

    字段定义集中在服务端，前端只负责渲染，避免两边各维护一份而逐渐走样。
    """
    # 默认值取自与生产知识库对齐的配置
    return jsonify({"fields": CONFIG_FIELDS, "defaults": dict(DEFAULT_PARSER_CONFIG)})


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
    """上传文件并启动 MinerU 解析，立即返回任务标识。

    解析耗时数分钟，必须异步：同步等待会让请求超时、界面假死。
    产物落在实验室自己的目录，与生产的 MinerU 输出彻底分开。
    """
    # 取上传的文件
    file = request.files.get("file")
    # 没有文件时明确提示
    if file is None or not file.filename:
        return jsonify({"ok": False, "message": "未选择文件"}), 400
    # 确保上传目录存在
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # 保留原始文件名：切分器按扩展名分派类型，改名会导致走错分支
    dest = UPLOAD_DIR / Path(file.filename).name
    # 落盘
    file.save(dest)
    # 启动后台解析
    task_id = start_parse(
        dest,  # 已落盘的文件路径
        Path(file.filename).name,  # 原始文件名
        backend=request.form.get("backend") or "pipeline",  # 处理后端类型
        parse_method=request.form.get("parse_method") or "auto",  # 解析方法
        auto_import=request.form.get("auto_import", "1") != "0",  # 解析后是否自动入库
        kind=request.form.get("kind", ""),  # 文档大类
        note=request.form.get("note", ""),  # 备注
    )
    # 返回任务标识供前端轮询
    return jsonify({"ok": True, "task_id": task_id})


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
    """删除某一轮快照。"""
    # 执行删除；若它是当前基准，指针会被一并清空
    runs.delete_run(run_id)
    # 返回成功
    return jsonify({"ok": True})


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
    # 生成预览数据
    return jsonify(inspect_case(cases[0], cfg=cfg, parser_config=config))


def serve(host="127.0.0.1", port=5099, debug=False):
    """启动本地服务。默认只监听回环地址，不对外暴露。"""
    # 启动时把旧版单文件基线收编为首个历史轮次，保证升级前后基准不断档
    migrated = runs.migrate_legacy_baseline()
    # 确有迁移时提示，便于使用者知道历史从何而来
    if migrated:
        print(f"已把旧版基线迁移为历史轮次：{migrated}")
    # 提示访问地址，便于直接点击打开
    print(f"chunk-lab 控制台：http://{host}:{port}")
    # 启动 Flask 开发服务器；关闭 reloader 避免语料评估被重复触发
    app.run(host=host, port=port, debug=debug, use_reloader=False)
