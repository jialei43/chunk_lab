"""chunk-lab 的本地 Web 服务。

只做三件事：把现有能力暴露成 HTTP 接口、托管单页前端、缓存最近一次评估结果。
不引入任何新依赖——Flask 来自 ragflow 的虚拟环境，前端是零构建的原生页面。

服务仅监听本机回环地址，是开发时工具，不面向网络暴露。
"""

import threading  # 导入 threading 用一把锁串行化耗时评估，避免并发重复计算
from pathlib import Path  # 导入 Path 定位前端静态文件

from flask import Flask, jsonify, request, send_from_directory  # 导入 Flask 及其响应工具

from .detectors import DetectorConfig  # 导入检测阈值配置
from .discover import scan_products  # 导入本机产物扫描
from .evaluate import (compare_reports, evaluate_all, inspect_case, load_baseline,
                       load_cases, save_baseline)  # 导入评估相关能力
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


@app.post("/api/eval")
def api_eval():
    """跑一轮全量评估，可选与基线对比。"""
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
        # 缓存结果供其它视图复用
        _last_report = report
        # 组装响应
        result = {"ok": True, "report": report}
        # 按需与基线对比
        if payload.get("compare"):
            # 读取基线
            baseline = load_baseline()
            # 基线存在才计算升降
            if baseline:
                # 附上对比结果
                result["comparison"] = compare_reports(baseline, report)
            else:
                # 明确告知尚无基线，前端据此提示
                result["comparison"] = None
        # 按需把本轮结果固化为新基线
        if payload.get("set_baseline"):
            # 写出基线文件
            save_baseline(report)
            # 标记已更新
            result["baseline_updated"] = True
        # 返回结果
        return jsonify(result)
    finally:
        # 释放评估锁
        _eval_lock.release()


@app.post("/api/baseline")
def api_set_baseline():
    """把最近一次评估结果固化为基线。"""
    # 尚未跑过评估时无从固化
    if _last_report is None:
        return jsonify({"ok": False, "message": "请先运行一次评估"}), 400
    # 写出基线
    path = save_baseline(_last_report)
    # 返回保存位置
    return jsonify({"ok": True, "path": str(path)})


@app.get("/api/baseline")
def api_get_baseline():
    """返回当前基线的摘要，供前端显示对比基准。"""
    # 读取基线
    baseline = load_baseline()
    # 不存在时明确返回空
    if baseline is None:
        return jsonify({"exists": False})
    # 剥离每个样本的 findings 明细：概览页只用逐样本统计，明细在切片预览里按需加载，
    # 带上会让响应体积膨胀数十倍而没有对应收益
    cases = [
        {k: v for k, v in c.items() if k != "findings"}
        for c in baseline.get("cases", [])
    ]
    # 返回可直接渲染的完整结构，使页面打开即有内容而不是空白等待
    return jsonify({
        "exists": True,  # 基线存在
        "generated_at": baseline.get("generated_at", ""),  # 生成时间
        "config": baseline.get("config", {}),  # 生成基线时使用的参数
        "case_count": baseline.get("case_count", 0),  # 样本数
        "chunk_total": baseline.get("chunk_total", 0),  # 切片总数
        "finding_total": baseline.get("finding_total", 0),  # 命中总数
        "totals_by_detector": baseline.get("totals_by_detector", {}),  # 各检测器命中
        "cases": cases,  # 逐样本统计
    })


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
    # 提示访问地址，便于直接点击打开
    print(f"chunk-lab 控制台：http://{host}:{port}")
    # 启动 Flask 开发服务器；关闭 reloader 避免语料评估被重复触发
    app.run(host=host, port=port, debug=debug, use_reloader=False)
