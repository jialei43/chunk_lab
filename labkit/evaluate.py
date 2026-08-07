"""评估编排：把语料库、离线切分与检测器串成一次完整评估。

产出同时面向两类消费者：
  - 人：终端表格，快速看清哪个样本、哪类问题最严重；
  - 机器：JSON 报告，作为回归基线用于比较两次代码改动之间的指标升降。
"""

import json  # 导入 json 输出机器可读报告
from dataclasses import asdict  # 导入 asdict 把检测结果转成可序列化字典
from datetime import datetime  # 导入 datetime 给报告打时间戳

import yaml  # 导入 yaml 读取语料描述文件

from .detectors import DetectorConfig, run_detectors  # 导入检测配置与执行入口
from .offline import normalize_chunks, run_offline_chunking  # 导入离线切分驱动
from .paths import BASELINE_DIR, CORPUS_DIR, REPORT_DIR  # 导入语料、基线与报告目录常量


def load_cases(only=None):
    """扫描语料库，返回全部样本描述。

    only 给定时只加载指定的 case_id，便于针对单个样本快速迭代。
    """
    # 收集样本描述
    cases = []
    # 语料目录尚未建立时直接返回空列表，交由调用方提示先导入语料
    if not CORPUS_DIR.is_dir():
        return cases
    # 按目录名排序遍历，保证每次运行的样本顺序稳定，报告可直接比对
    for case_dir in sorted(CORPUS_DIR.iterdir()):
        # 跳过非目录项，例如 .gitkeep
        if not case_dir.is_dir():
            continue
        # 描述文件路径
        meta_path = case_dir / "case.yaml"
        # 产物文件路径
        json_path = case_dir / "content_list.json"
        # 两者缺一则该目录不是完整样本，跳过
        if not meta_path.is_file() or not json_path.is_file():
            continue
        # 读取描述文件
        with meta_path.open("r", encoding="utf-8") as fh:
            # 用 safe_load 避免执行任意标签
            meta = yaml.safe_load(fh)
        # 指定了筛选条件且当前样本不在其中时跳过
        if only and meta.get("case_id") not in only:
            continue
        # 把产物路径补进描述，供后续切分使用
        meta["content_list"] = json_path
        # 收入结果
        cases.append(meta)
    # 返回全部样本
    return cases


def evaluate_case(case, cfg=None, children_delimiter=""):
    """评估单个样本：离线切分 + 全部检测器，返回统计与命中。"""
    # 未指定配置时使用默认阈值
    cfg = cfg or DetectorConfig()
    # 执行离线切分；切分失败应作为样本级问题上报而不是中断整轮评估
    try:
        # 按样本描述中的参数调用离线驱动
        chunks = run_offline_chunking(
            case["content_list"],  # 缓存产物路径
            case["filename"],  # 原始文件名，决定切分器的类型判断
            parser_config={  # 本次评估的切分配置
                "children_delimiter": children_delimiter,  # 父子分块分隔符
                "chunk_token_num": cfg.chunk_token_num,  # token 预算与检测阈值保持同一来源
            },
            slide_mode=bool(case.get("slide_mode")),  # PPTX 按页切分开关
        )
    except Exception as e:
        # 切分崩溃是最严重的问题，直接作为一条结果返回
        return {
            "case_id": case["case_id"],  # 样本标识
            "kind": case.get("kind", ""),  # 文档大类
            "error": f"{type(e).__name__}: {e}",  # 异常信息
            "chunk_count": 0,  # 无产出
            "findings": [],  # 无命中
        }
    # 规范化为可序列化记录
    records = normalize_chunks(chunks)
    # 跑全部检测器
    findings = run_detectors(records, case["case_id"], cfg)
    # 按检测器名统计命中数，作为该样本的指标向量
    by_detector = {}
    # 逐条累计
    for f in findings:
        # 累加对应检测器的计数
        by_detector[f.detector] = by_detector.get(f.detector, 0) + 1
    # 组装该样本的评估结果
    return {
        "case_id": case["case_id"],  # 样本标识
        "kind": case.get("kind", ""),  # 文档大类
        "chunk_count": len(records),  # 产出的 chunk 总数
        "block_count": case.get("block_count", 0),  # MinerU 原始块数，用于观察合并率
        "by_detector": by_detector,  # 各检测器命中数，回归比较的核心指标
        "finding_count": len(findings),  # 命中总数
        "findings": [asdict(f) for f in findings],  # 命中明细，供人工复核
    }


def evaluate_all(only=None, cfg=None, children_delimiter=""):
    """评估全部语料，返回完整报告字典。"""
    # 未指定配置时使用默认阈值
    cfg = cfg or DetectorConfig()
    # 加载语料
    cases = load_cases(only=only)
    # 逐个评估
    results = [evaluate_case(c, cfg=cfg, children_delimiter=children_delimiter) for c in cases]
    # 汇总各检测器在全语料上的总命中数，这是判断改动优劣的顶层指标
    totals = {}
    # 逐样本累加
    for r in results:
        # 逐检测器累加
        for name, count in (r.get("by_detector") or {}).items():
            # 累加到全局计数
            totals[name] = totals.get(name, 0) + count
    # 组装报告
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),  # 生成时间，便于追溯
        "config": {  # 本轮使用的参数，报告之间比较时必须确认参数一致
            "chunk_token_num": cfg.chunk_token_num,  # token 预算
            "children_delimiter": children_delimiter,  # 父子分块分隔符
            "oversize_ratio": cfg.oversize_ratio,  # 超长判据倍数
        },
        "case_count": len(results),  # 评估的样本数
        "chunk_total": sum(r.get("chunk_count", 0) for r in results),  # 全语料 chunk 总数
        "finding_total": sum(r.get("finding_count", 0) for r in results),  # 全语料命中总数
        "totals_by_detector": totals,  # 各检测器全局命中数
        "cases": results,  # 逐样本明细
    }


def save_report(report, name=None):
    """把报告写入 reports/ 目录，返回文件路径。"""
    # 确保报告目录存在
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    # 未指定名称时用时间戳命名，避免覆盖历史报告
    name = name or f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    # 目标路径
    path = REPORT_DIR / name
    # 写出 JSON，ensure_ascii=False 保留中文可读性
    with path.open("w", encoding="utf-8") as fh:
        # indent=2 便于人工查看与 git diff
        json.dump(report, fh, ensure_ascii=False, indent=2)
    # 返回路径供调用方提示
    return path


def save_baseline(report, name="current.json"):
    """把报告存为回归基线。基线纳入 chunk-lab 版本管理，指标变化因此可追溯。"""
    # 确保基线目录存在
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    # 目标路径
    path = BASELINE_DIR / name
    # 写出 JSON
    with path.open("w", encoding="utf-8") as fh:
        # 与报告同格式，便于直接比较
        json.dump(report, fh, ensure_ascii=False, indent=2)
    # 返回路径
    return path


def load_baseline(name="current.json"):
    """读取回归基线，不存在时返回 None 由调用方提示先建立基线。"""
    # 基线路径
    path = BASELINE_DIR / name
    # 不存在直接返回空
    if not path.is_file():
        return None
    # 读取并返回
    with path.open("r", encoding="utf-8") as fh:
        # 解析 JSON
        return json.load(fh)


def compare_reports(baseline, current):
    """比较基线与当前报告，返回逐检测器与逐样本的升降。

    这是"敢改代码"的依据：修好一个样本的同时是否弄坏了别的样本，
    只有把全语料指标并排比较才能看出来。
    """
    # 收集两侧出现过的全部检测器名
    names = set(baseline.get("totals_by_detector", {})) | set(current.get("totals_by_detector", {}))
    # 逐检测器计算变化量
    by_detector = []
    # 遍历全部检测器
    for name in sorted(names):
        # 基线命中数，缺失按 0 计
        before = baseline.get("totals_by_detector", {}).get(name, 0)
        # 当前命中数
        after = current.get("totals_by_detector", {}).get(name, 0)
        # 无变化的项不进入结果，保持输出聚焦
        if before != after:
            # 记录变化
            by_detector.append({"detector": name, "before": before, "after": after, "delta": after - before})

    # 按样本比较命中总数与 chunk 数，定位变化发生在哪个文档
    base_cases = {c["case_id"]: c for c in baseline.get("cases", [])}
    # 逐样本比较
    by_case = []
    # 遍历当前报告的样本
    for c in current.get("cases", []):
        # 找到基线中的对应样本，缺失说明是新增语料
        b = base_cases.get(c["case_id"])
        # 新增样本无从比较，跳过
        if not b:
            continue
        # 命中数或 chunk 数任一变化都值得记录
        if b.get("finding_count") != c.get("finding_count") or b.get("chunk_count") != c.get("chunk_count"):
            # 记录该样本的变化
            by_case.append({
                "case_id": c["case_id"],  # 样本标识
                "finding_before": b.get("finding_count", 0),  # 变化前命中数
                "finding_after": c.get("finding_count", 0),  # 变化后命中数
                "chunk_before": b.get("chunk_count", 0),  # 变化前 chunk 数
                "chunk_after": c.get("chunk_count", 0),  # 变化后 chunk 数
            })
    # 汇总比较结果
    return {
        "baseline_at": baseline.get("generated_at", ""),  # 基线生成时间
        "current_at": current.get("generated_at", ""),  # 当前报告生成时间
        "finding_before": baseline.get("finding_total", 0),  # 基线总命中
        "finding_after": current.get("finding_total", 0),  # 当前总命中
        "by_detector": by_detector,  # 逐检测器变化
        "by_case": by_case,  # 逐样本变化
    }


def format_comparison(cmp):
    """把比较结果渲染成终端文本，劣化项显式标注以便一眼看见。"""
    # 逐行累积
    lines = []
    # 标题与总量变化
    lines.append("")
    lines.append("=" * 78)
    lines.append(f"【回归对比】基线 {cmp['baseline_at']} → 当前 {cmp['current_at']}")
    # 总命中变化量
    delta = cmp["finding_after"] - cmp["finding_before"]
    # 用符号直观表达方向：命中减少是改善，增加是劣化
    arrow = "改善" if delta < 0 else ("劣化" if delta > 0 else "持平")
    lines.append(f"总命中 {cmp['finding_before']} → {cmp['finding_after']}（{delta:+d}，{arrow}）")
    lines.append("=" * 78)

    # 无任何变化时直接说明，避免使用者怀疑没跑成功
    if not cmp["by_detector"] and not cmp["by_case"]:
        lines.append("  与基线完全一致，无指标变化")
        return "\n".join(lines)

    # 逐检测器变化
    if cmp["by_detector"]:
        lines.append("")
        lines.append("【按检测器】")
        # 按变化量排序，劣化最严重的排最前
        for d in sorted(cmp["by_detector"], key=lambda x: -x["delta"]):
            # 劣化项加显著标记，便于在 CI 或终端里一眼捕捉
            mark = "  ← 劣化" if d["delta"] > 0 else ""
            # 输出一行
            lines.append(f"  {d['detector']:<24} {d['before']:>5} → {d['after']:<5} ({d['delta']:+d}){mark}")

    # 逐样本变化
    if cmp["by_case"]:
        lines.append("")
        lines.append("【按样本】")
        # 逐条输出
        for c in sorted(cmp["by_case"], key=lambda x: -(x["finding_after"] - x["finding_before"])):
            # 命中变化量
            fd = c["finding_after"] - c["finding_before"]
            # chunk 数变化量，切分粒度是否被意外改变全靠它暴露
            cd = c["chunk_after"] - c["chunk_before"]
            # 劣化标记
            mark = "  ← 劣化" if fd > 0 else ""
            # 输出一行，同时展示命中与 chunk 数的变化
            lines.append(f"  {c['case_id']:<20} 命中 {c['finding_before']:>4}→{c['finding_after']:<4}({fd:+d})   "
                         f"chunk {c['chunk_before']:>4}→{c['chunk_after']:<4}({cd:+d}){mark}")
    # 合并输出
    return "\n".join(lines)


def format_report(report, top_findings=0):
    """把报告渲染成终端表格文本。"""
    # 逐行累积输出内容
    lines = []
    # 报告头部：总体规模
    lines.append("=" * 78)
    lines.append(f"语料 {report['case_count']} 个   chunk {report['chunk_total']} 个   命中 {report['finding_total']} 条")
    lines.append(f"参数 chunk_token_num={report['config']['chunk_token_num']}  "
                 f"children_delimiter={report['config']['children_delimiter']!r}")
    lines.append("=" * 78)

    # 各检测器全局命中，按命中数降序，最严重的问题排在最前
    lines.append("")
    lines.append("【全局命中分布】")
    # 无命中时明确说明，避免空白造成误解
    if not report["totals_by_detector"]:
        lines.append("  （无命中）")
    # 按命中数从多到少排列
    for name, count in sorted(report["totals_by_detector"].items(), key=lambda x: -x[1]):
        # 每个检测器一行
        lines.append(f"  {name:<24} {count:>6}")

    # 逐样本明细表
    lines.append("")
    lines.append("【逐样本】")
    # 表头
    lines.append(f"  {'case_id':<20}{'kind':<16}{'块→chunk':>14}{'命中':>8}")
    lines.append("  " + "-" * 58)
    # 逐样本一行
    for r in report["cases"]:
        # 切分失败的样本单独标注
        if r.get("error"):
            lines.append(f"  {r['case_id']:<20}{r.get('kind', ''):<16}{'切分失败':>14}  {r['error'][:30]}")
            continue
        # 正常样本展示块数到 chunk 数的变化，便于观察合并率是否异常
        ratio = f"{r.get('block_count', 0)}→{r['chunk_count']}"
        # 输出一行
        lines.append(f"  {r['case_id']:<20}{r.get('kind', ''):<16}{ratio:>14}{r['finding_count']:>8}")

    # 按需展示部分命中明细，便于直接看到问题长什么样
    if top_findings:
        lines.append("")
        lines.append(f"【命中样例（每类最多 {top_findings} 条）】")
        # 按检测器归组
        grouped = {}
        # 遍历所有样本的命中
        for r in report["cases"]:
            # 逐条归入对应检测器
            for f in r.get("findings", []):
                # 建组并追加
                grouped.setdefault(f["detector"], []).append(f)
        # 按组输出
        for name, items in sorted(grouped.items(), key=lambda x: -len(x[1])):
            # 组标题带总数
            lines.append("")
            lines.append(f"  ▸ {name}（共 {len(items)} 条）")
            # 每组只展示前若干条
            for f in items[:top_findings]:
                # 问题描述行
                lines.append(f"      [{f['case_id']} #{f['chunk_index']}] {f['message']}")
                # 证据行，存在时才输出
                if f.get("evidence"):
                    # 证据可能含换行，压平成单行避免破坏表格结构
                    ev = f["evidence"].replace("\n", "⏎")
                    # 限制长度防止刷屏
                    lines.append(f"        {ev[:100]}")
    # 合并成最终文本
    return "\n".join(lines)
