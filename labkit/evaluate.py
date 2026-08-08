"""评估编排：把语料库、离线切分与检测器串成一次完整评估。

产出同时面向两类消费者：
  - 人：终端表格，快速看清哪个样本、哪类问题最严重；
  - 机器：JSON 报告，作为回归基线用于比较两次代码改动之间的指标升降。
"""

import json  # 导入 json 输出机器可读报告
from dataclasses import asdict  # 导入 asdict 把检测结果转成可序列化字典
from datetime import datetime  # 导入 datetime 给报告打时间戳

import yaml  # 导入 yaml 读取语料描述文件

import logging  # 导入 logging 记录归因等辅助步骤的降级情况

from .attribution import attribute_findings, summarize_attribution  # 导入问题归因
from .detectors import DetectorConfig, run_detectors  # 导入检测配置与执行入口
from .offline import build_parser_config, normalize_chunks, run_offline_chunking  # 导入离线切分驱动
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
        # 产物文件按 MinerU 原始命名保存，用通配定位而非固定文件名
        candidates = list(case_dir.glob("*_content_list.json"))
        # 两者缺一则该目录不是完整样本，跳过
        if not meta_path.is_file() or not candidates:
            continue
        # 读取描述文件
        with meta_path.open("r", encoding="utf-8") as fh:
            # 用 safe_load 避免执行任意标签
            meta = yaml.safe_load(fh)
        # 指定了筛选条件且当前样本不在其中时跳过
        if only and meta.get("case_id") not in only:
            continue
        # 把产物路径补进描述，供后续切分使用
        meta["content_list"] = candidates[0]
        # 样本目录本身即桥接层需要的语料目录
        meta["corpus_dir"] = case_dir
        # 收入结果
        cases.append(meta)
    # 返回全部样本
    return cases


def evaluate_case(case, cfg=None, parser_config=None):
    """评估单个样本：离线切分 + 全部检测器，返回统计与命中。"""
    # 未指定配置时使用默认阈值
    cfg = cfg or DetectorConfig()
    # 执行离线切分；切分失败应作为样本级问题上报而不是中断整轮评估
    try:
        # 整份配置直接透传，字段的合并与派生由 offline.build_parser_config 统一处理
        chunks = run_offline_chunking(
            case["content_list"],  # 缓存产物路径
            case["filename"],  # 原始文件名，决定切分器的类型判断
            parser_config=parser_config,  # 完整 parser_config，与知识库配置同构
            backend=case.get("backend", "hybrid_auto"),  # 产物 backend，决定 _read_output 的查找
        )
    except Exception as e:
        # 切分崩溃是最严重的问题，直接作为一条结果返回
        return {
            "case_id": case["case_id"],  # 样本标识
            "filename": case.get("filename", ""),  # 原始中文文件名
            "note": case.get("note", ""),  # 该样本的关注点说明
            "kind": case.get("kind", ""),  # 文档大类
            "error": f"{type(e).__name__}: {e}",  # 异常信息
            "chunk_count": 0,  # 无产出
            "findings": [],  # 无命中
        }
    # 规范化为可序列化记录
    records = normalize_chunks(chunks)
    # 跑全部检测器
    findings = run_detectors(records, case["case_id"], cfg)
    # 补充归因：把问题对照原始块，判断该改切分代码还是解析/噪声治理。
    # 不做这个区分，报告里每条都指向 mineru_chunker，而其中不少改那里修不好。
    finding_dicts = [asdict(f) for f in findings]
    # 归因失败不应影响评估主流程
    try:
        # 加载该样本的原始块（含 middle.json 增强），与切分时同一份输入
        from chunklab_bridge.bridge import load_blocks
        _parser, blocks = load_blocks(case["corpus_dir"], backend=case.get("backend", "hybrid_auto"))
        # 逐条补充归因结论与修复建议
        finding_dicts = attribute_findings(finding_dicts, blocks, records)
    except Exception as e:
        # 记录但不中断，报告中该字段缺失即可
        logging.warning(f"[chunk-lab] 归因失败 case={case.get('case_id')}: {e}")
    # 按检测器名统计命中数，作为该样本的指标向量
    by_detector = {}
    # 逐条累计
    for f in findings:
        # 累加对应检测器的计数
        by_detector[f.detector] = by_detector.get(f.detector, 0) + 1
    # 组装该样本的评估结果
    return {
        "case_id": case["case_id"],  # 样本标识
        "filename": case.get("filename", ""),  # 原始中文文件名，便于在报告中辨认是哪份文档
        "note": case.get("note", ""),  # 该样本的关注点说明
        "kind": case.get("kind", ""),  # 文档大类
        # 切分文本随结果带出，由 save_run 取走单独压缩存储。
        # 代码一改旧结果就无法重现，不存下来历史轮次就只剩数字。
        "_records": records,  # 本样本的全部切片记录
        "chunk_count": len(records),  # 产出的 chunk 总数
        "block_count": case.get("block_count", 0),  # MinerU 原始块数，用于观察合并率
        "by_detector": by_detector,  # 各检测器命中数，回归比较的核心指标
        "finding_count": len(findings),  # 命中总数
        "findings": finding_dicts,  # 命中明细（含归因），供人工复核
        # 归因汇总：直接回答「这些问题里有多少真该改切分代码」
        "attribution": summarize_attribution(finding_dicts),
    }


def inspect_case(case, cfg=None, parser_config=None):
    """返回单个样本的完整切片内容，并把检测命中挂到对应切片上。

    这是「预览切分效果」的数据源：前端据此逐块展示正文，
    并在有问题的块上直接标注是哪类缺陷、证据是什么。
    """
    # 未指定配置时使用默认阈值
    cfg = cfg or DetectorConfig()
    # 执行离线切分；失败时把错误回传给前端而不是抛出 500
    try:
        # 按样本描述中的参数切分
        chunks = run_offline_chunking(
            case["content_list"],  # 缓存产物路径
            case["filename"],  # 原始文件名
            parser_config=parser_config,  # 完整 parser_config，与评估口径一致
            backend=case.get("backend", "hybrid_auto"),  # 产物 backend
        )
    except Exception as e:
        # 以结构化错误返回，前端可直接展示
        return {"case_id": case["case_id"], "error": f"{type(e).__name__}: {e}", "chunks": []}
    # 规范化为可序列化记录
    records = normalize_chunks(chunks)
    # 跑全部检测器
    findings = run_detectors(records, case["case_id"], cfg)
    # 按 chunk 序号归集命中，便于挂载到对应切片
    by_index = {}
    # 逐条归入对应切片
    for f in findings:
        # 同一切片可能命中多个检测器
        by_index.setdefault(f.chunk_index, []).append(asdict(f))
    # 逐块组装前端需要的结构
    items = []
    # 遍历全部切片
    for r in records:
        # 复制一份记录并挂上该块的问题列表
        item = dict(r)
        # 没有命中时为空列表，前端据此判断是否高亮
        item["findings"] = by_index.get(r["index"], [])
        # 父块正文体积可能很大，预览无需重复传输，只保留是否为子块的标记
        item.pop("mom_content", None)
        # 收入结果
        items.append(item)
    # 返回该样本的完整预览数据
    return {
        "case_id": case["case_id"],  # 样本标识
        "filename": case.get("filename", ""),  # 原始文件名
        "kind": case.get("kind", ""),  # 文档大类
        "chunk_count": len(items),  # 切片总数
        "finding_count": len(findings),  # 命中总数
        "chunks": items,  # 逐块内容与标注
    }


def evaluate_all(only=None, cfg=None, parser_config=None):
    """评估全部语料，返回完整报告字典。"""
    # 未指定配置时使用默认阈值
    cfg = cfg or DetectorConfig()
    # 解析本轮实际生效的完整配置，供报告记录与可比性校验
    effective_config = build_parser_config(parser_config)
    # 加载语料
    cases = load_cases(only=only)
    # 逐个评估
    results = [evaluate_case(c, cfg=cfg, parser_config=parser_config) for c in cases]
    # 把切分文本从逐样本结果中抽出，集中交给 save_run 压缩存储，
    # 避免它随报告 JSON 一起落盘把文件撑大
    chunks_by_case = {}
    # 逐样本取出并从结果中移除
    for r in results:
        # 切分失败的样本没有记录
        recs = r.pop("_records", None)
        # 有记录才收集
        if recs:
            chunks_by_case[r["case_id"]] = recs
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
        # 本轮实际生效的完整切分配置。记录全量而非若干字段，
        # 否则两轮对比时无法判断差异是否来自未记录的参数。
        "config": {**effective_config, "oversize_ratio": cfg.oversize_ratio},
        "case_count": len(results),  # 评估的样本数
        "chunk_total": sum(r.get("chunk_count", 0) for r in results),  # 全语料 chunk 总数
        "finding_total": sum(r.get("finding_count", 0) for r in results),  # 全语料命中总数
        "totals_by_detector": totals,  # 各检测器全局命中数
        "cases": results,  # 逐样本明细
        "_chunks": chunks_by_case,  # 切分文本，save_run 会取走并单独压缩存储
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
    # 校验两轮是否真的可比：参数或语料不同的两轮，数字并列没有意义
    warnings = []
    # 取两侧的运行参数
    cfg_a, cfg_b = baseline.get("config") or {}, current.get("config") or {}
    # token 预算不同会直接改变切分粒度，命中数的增减无法归因于代码改动
    if cfg_a.get("chunk_token_num") != cfg_b.get("chunk_token_num"):
        warnings.append(
            f"token 预算不同（{cfg_a.get('chunk_token_num')} vs {cfg_b.get('chunk_token_num')}），"
            f"切分粒度本就不同，数字不能直接归因于代码改动")
    # 父子分隔符决定按段落还是按标题划界，影响同样巨大
    if cfg_a.get("children_delimiter") != cfg_b.get("children_delimiter"):
        warnings.append(
            f"子分隔符不同（{cfg_a.get('children_delimiter')!r} vs {cfg_b.get('children_delimiter')!r}），"
            f"切分边界口径不同，不可直接比较")
    # 语料增删会改变总量，此时只应看逐样本变化而非总数
    if baseline.get("corpus_hash") and current.get("corpus_hash") and baseline["corpus_hash"] != current["corpus_hash"]:
        warnings.append("语料构成已变化，总数对比无意义，请只看两侧都存在的样本")
    # 代码指纹相同却出现差异，说明变化另有来源（语料或参数），值得提醒
    code_a = (baseline.get("code") or {}).get("hash")
    code_b = (current.get("code") or {}).get("hash")
    # 仅在两侧都有指纹时判断
    if code_a and code_b and code_a == code_b and by_detector:
        warnings.append("两轮的被测代码完全相同，指标差异并非来自代码改动")

    # 汇总比较结果
    return {
        "baseline_at": baseline.get("generated_at", ""),  # 基线生成时间
        "current_at": current.get("generated_at", ""),  # 当前报告生成时间
        "baseline_run_id": baseline.get("run_id", ""),  # 基准轮次标识
        "current_run_id": current.get("run_id", ""),  # 当前轮次标识
        "baseline_label": baseline.get("label", ""),  # 基准轮次备注
        "current_label": current.get("label", ""),  # 当前轮次备注
        "baseline_code": baseline.get("code") or {},  # 基准的代码指纹
        "current_code": current.get("code") or {},  # 当前的代码指纹
        "chunk_before": baseline.get("chunk_total", 0),  # 基准切片总数
        "chunk_after": current.get("chunk_total", 0),  # 当前切片总数
        "finding_before": baseline.get("finding_total", 0),  # 基线总命中
        "finding_after": current.get("finding_total", 0),  # 当前总命中
        "warnings": warnings,  # 可比性警告，前端必须显著展示
        "by_detector": by_detector,  # 逐检测器变化
        "by_case": by_case,  # 逐样本变化
    }


def _finding_identity(case_id, finding):
    """给一条问题生成跨轮次稳定的身份标识。

    不能用 chunk_index：切分逻辑一改，序号整体错位，同一个问题会被算成
    「旧的消失了 + 新的出现了」，差异列表里全是噪声。
    证据文本取自原文片段，切分边界变动时它基本不变，是更可靠的身份。
    证据为空时才回落到序号，此时精度有限但总比没有强。
    """
    # 取证据文本并去除首尾空白
    ev = (finding.get("evidence") or "").strip()
    # 有证据时用「样本 + 检测器 + 证据」作为身份
    if ev:
        return (case_id, finding.get("detector"), ev)
    # 无证据时回落到序号，并显式标记以便调用方知道匹配可能不准
    return (case_id, finding.get("detector"), f"#idx{finding.get('chunk_index')}")


def diff_findings(before, after, detector=None, case_id=None):
    """列出两轮之间具体新增与消失的问题条目。

    这是从「+6」下钻到「是哪 6 条」的关键：只给变化量而不能定位到具体条目，
    就无法按问题着手修复。
    """
    # 把一轮报告展开成 身份 -> 条目 的映射
    def index_of(report):
        # 收集该轮的全部问题条目
        out = {}
        # 逐样本遍历
        for case in report.get("cases", []):
            # 指定样本时只看该样本
            cid = case.get("case_id")
            if case_id and cid != case_id:
                continue
            # 逐条问题
            for f in case.get("findings", []):
                # 指定检测器时只看该类问题
                if detector and f.get("detector") != detector:
                    continue
                # 以稳定身份为键；同一身份重复出现时保留首条即可
                out.setdefault(_finding_identity(cid, f), {**f, "case_id": cid})
        # 返回映射
        return out

    # 分别建立两轮的索引
    idx_a, idx_b = index_of(before), index_of(after)
    # B 有而 A 没有的是新增问题
    added = [idx_b[k] for k in idx_b.keys() - idx_a.keys()]
    # A 有而 B 没有的是已消失的问题
    removed = [idx_a[k] for k in idx_a.keys() - idx_b.keys()]
    # 按样本与序号排序，便于顺序阅读
    added.sort(key=lambda f: (f.get("case_id", ""), f.get("chunk_index", 0)))
    removed.sort(key=lambda f: (f.get("case_id", ""), f.get("chunk_index", 0)))
    # 返回差异结果
    return {
        "detector": detector or "",  # 本次下钻的检测器
        "case_id": case_id or "",  # 本次下钻的样本
        # 两轮各自的切分参数：条目里的 chunk_index 只在对应参数下才成立，
        # 前端据此用同样的参数重新切分，才能精确定位到那一块
        "before_config": before.get("config") or {},  # 基准轮的参数，removed 条目属于它
        "after_config": after.get("config") or {},  # 对比轮的参数，added 条目属于它
        "before_run_id": before.get("run_id", ""),  # 基准轮标识
        "after_run_id": after.get("run_id", ""),  # 对比轮标识
        "added": added,  # 新增的问题条目
        "removed": removed,  # 消失的问题条目
        "added_count": len(added),  # 新增数量
        "removed_count": len(removed),  # 消失数量
        "net": len(added) - len(removed),  # 净变化，应与对比表中的 delta 一致
    }


def diff_chunk_texts(chunks_a, chunks_b, limit=200):
    """对比两轮的切分文本，按内容配对，找出真正变化的切片。

    这是保留文本快照的核心价值：问题数只能告诉你「多了 6 条截断」，
    文本对比才能告诉你「这一段的边界从哪挪到了哪」「内容被改成了什么样」。

    配对策略与 diff_findings 同理——不能按序号配，序号会整体错位。
    这里按正文内容配对：完全相同的算未变，剩下的按顺序配成「修改」，
    多出来的算新增或删除。
    """
    # 防御：任一侧缺失文本快照时无法比较
    if chunks_a is None or chunks_b is None:
        return None
    # 建立正文到序号列表的映射，用于快速识别未变化的切片
    def by_content(chunks):
        # 同一正文可能出现多次，故值为序号列表
        m = {}
        for c in chunks:
            m.setdefault(c["content"], []).append(c)
        return m

    # 两侧的内容映射
    map_a, map_b = by_content(chunks_a), by_content(chunks_b)
    # 两侧共有的正文即未变化部分
    common = map_a.keys() & map_b.keys()
    # 计算未变化的切片数：取两侧出现次数的较小值
    unchanged = sum(min(len(map_a[k]), len(map_b[k])) for k in common)

    # 剩余部分按原顺序排列，准备配对
    rest_a = [c for c in chunks_a if c["content"] not in common]
    rest_b = [c for c in chunks_b if c["content"] not in common]
    # 逐位配对成「修改」，超出的部分是纯新增或纯删除
    changed = []
    # 取较短长度作为配对数量
    n = min(len(rest_a), len(rest_b))
    # 逐对生成修改条目
    for i in range(min(n, limit)):
        # 记录两侧内容，前端据此做逐字对比
        changed.append({
            "kind": "modified",  # 变化类型：内容被修改
            "before_index": rest_a[i]["index"],  # 变化前的序号
            "after_index": rest_b[i]["index"],  # 变化后的序号
            "before": rest_a[i]["content"],  # 变化前的正文
            "after": rest_b[i]["content"],  # 变化后的正文
        })
    # A 侧多出的是被删除的切片
    for c in rest_a[n:n + limit]:
        changed.append({
            "kind": "removed",  # 变化类型：该切片消失了
            "before_index": c["index"],  # 原序号
            "after_index": None,  # 无对应
            "before": c["content"],  # 原正文
            "after": "",  # 无内容
        })
    # B 侧多出的是新增的切片
    for c in rest_b[n:n + limit]:
        changed.append({
            "kind": "added",  # 变化类型：新出现的切片
            "before_index": None,  # 无对应
            "after_index": c["index"],  # 新序号
            "before": "",  # 无内容
            "after": c["content"],  # 新正文
        })
    # 返回文本对比结果
    return {
        "count_a": len(chunks_a),  # 变化前的切片总数
        "count_b": len(chunks_b),  # 变化后的切片总数
        "unchanged": unchanged,  # 内容完全未变的切片数
        "changed": changed[:limit],  # 变化明细，受 limit 限制以免响应过大
        "changed_total": len(changed),  # 变化总数（未截断前）
        "truncated": len(changed) > limit,  # 是否因超限被截断
    }


def format_comparison(cmp):
    """把比较结果渲染成终端文本，劣化项显式标注以便一眼看见。"""
    # 逐行累积
    lines = []
    # 标题与总量变化；带上轮次标识与备注，便于确认比的是哪两轮
    lines.append("")
    lines.append("=" * 78)
    # 组装两侧的标识串：优先展示 run_id 与备注，回落到时间戳
    left = cmp.get("baseline_run_id") or cmp["baseline_at"]
    right = cmp.get("current_run_id") or cmp["current_at"]
    # 备注存在时附在标识后
    left += f"（{cmp['baseline_label']}）" if cmp.get("baseline_label") else ""
    right += f"（{cmp['current_label']}）" if cmp.get("current_label") else ""
    lines.append(f"【轮次对比】{left}  →  {right}")
    # 总命中变化量
    delta = cmp["finding_after"] - cmp["finding_before"]
    # 用符号直观表达方向：命中减少是改善，增加是劣化
    arrow = "改善" if delta < 0 else ("劣化" if delta > 0 else "持平")
    lines.append(f"总问题 {cmp['finding_before']} → {cmp['finding_after']}（{delta:+d}，{arrow}）　"
                 f"总切片 {cmp.get('chunk_before', 0)} → {cmp.get('chunk_after', 0)}")
    # 代码指纹对照：判断这次差异是否真的来自代码改动
    ca, cb = cmp.get("baseline_code") or {}, cmp.get("current_code") or {}
    # 两侧都有指纹时才展示
    if ca.get("hash") or cb.get("hash"):
        lines.append(f"代码指纹 {ca.get('hash', '?')} → {cb.get('hash', '?')}"
                     f"{'（当前含未提交改动）' if cb.get('git_dirty') else ''}")
    lines.append("=" * 78)

    # 可比性警告必须排在结论之前：参数或语料不同的两轮，数字并列会直接导致误判
    if cmp.get("warnings"):
        lines.append("")
        lines.append("⚠️  这两轮不完全可比：")
        # 逐条列出原因
        for w in cmp["warnings"]:
            lines.append(f"    · {w}")

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
