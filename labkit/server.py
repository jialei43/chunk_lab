"""chunk-lab 的本地 Web 服务。

只做三件事：把现有能力暴露成 HTTP 接口、托管单页前端、缓存最近一次评估结果。
不引入任何新依赖——Flask 来自 ragflow 的虚拟环境，前端是零构建的原生页面。

服务仅监听本机回环地址，是开发时工具，不面向网络暴露。
"""

import threading  # 导入 threading 用一把锁串行化耗时评估，避免并发重复计算
from pathlib import Path  # 导入 Path 定位前端静态文件

from flask import Flask, jsonify, request, send_from_directory  # 导入 Flask 及其响应工具

from . import runs  # 导入运行历史模块，负责轮次快照与基准指针
from .detectors import DetectorConfig  # 导入检测阈值配置
from .discover import scan_products  # 导入本机产物扫描
from .evaluate import compare_reports, diff_findings, evaluate_all, inspect_case, load_cases  # 导入评估相关能力
from .ingest import ingest_from_path  # 导入按路径导入语料的能力

# 前端静态文件目录
WEB_DIR = Path(__file__).resolve().parent / "web"

# Flask 应用实例，静态资源由自定义路由处理故不启用默认静态目录
app = Flask(__name__, static_folder=None)

# 评估互斥锁：评估耗时十余秒且会占满 CPU，同一时刻只允许跑一轮
_eval_lock = threading.Lock()
# 最近一次评估结果缓存，供前端切换视图时免于重复计算
_last_report = None


def _config_from_request(payload):
    """从请求体解析本轮运行参数，缺省时回落到默认值。"""
    # token 预算，同时作为超长判据基准
    chunk_token_num = int(payload.get("chunk_token_num") or 512)
    # 父子分块分隔符，对切分粒度影响极大
    children_delimiter = payload.get("children_delimiter") or ""
    # 组装检测配置并返回
    return DetectorConfig(chunk_token_num=chunk_token_num), children_delimiter


@app.get("/")
def index():
    """返回单页前端。"""
    # 直接发送 index.html
    return send_from_directory(WEB_DIR, "index.html")


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
    # 解析运行参数
    cfg, delimiter = _config_from_request(payload)
    # 限定评估的样本，为空表示全量
    only = payload.get("cases") or None
    # 上一轮尚未结束时拒绝新请求，避免 CPU 争抢导致两轮都变慢
    if not _eval_lock.acquire(blocking=False):
        return jsonify({"ok": False, "message": "已有评估正在运行，请稍候"}), 409
    # 确保无论成败都释放锁
    try:
        # 执行评估
        report = evaluate_all(only=only, cfg=cfg, children_delimiter=delimiter)
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
    # 从查询参数解析运行配置，使预览与评估口径一致
    cfg = DetectorConfig(chunk_token_num=int(request.args.get("chunk_token_num") or 512))
    # 父子分块分隔符
    delimiter = request.args.get("children_delimiter") or ""
    # 生成预览数据
    return jsonify(inspect_case(cases[0], cfg=cfg, children_delimiter=delimiter))


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
