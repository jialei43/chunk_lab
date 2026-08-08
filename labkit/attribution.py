"""问题归因：把可疑切片对照原始文档，判断问题究竟出在哪个环节。

这是「是不是真实切块问题」的关键。同样一条「句子截断」，成因可能完全不同：

  - 切分器在原始块**内部**切开了 → 确实是切分逻辑的问题，该改 mineru_chunker；
  - 切片边界与原始块边界**重合** → 切分器只是没把两个块合并，
    要么是 MinerU 上游就把这句话断成了两块（该改续排合并），
    要么是合并策略选择不合并（该改合并边界判定）；
  - 原始块本身就是脏的（目录条目、页眉页脚）→ 是解析与噪声治理的问题，
    改切分代码毫无用处。

不做这个区分，报告里的每一条都指向 mineru_chunker，而其中相当一部分
改那里根本修不好。
"""

import re  # 导入 re 做文本规范化

# 归因结论及其含义，同时作为报告中的展示文案
ATTRIBUTION_LABELS = {
    "inside_block": "切分器在原始块内部切开",
    "block_boundary": "切片边界与原始块边界重合",
    "dirty_block": "原始块本身就有问题",
    "unknown": "无法定位到原始块",
}

# 归因结论对应的建议修复方向，直接指向应该改哪里
ATTRIBUTION_ADVICE = {
    "inside_block": "属切分逻辑问题，查 rag/app/mineru_chunker.py 的 _merge_text_units / _split_oversized_text",
    "block_boundary": "切分器未合并相邻块。若两块本是同一句，查 deepdoc/parser/mineru_parser.py 的跨页续排合并；"
                      "否则查 _merge_text_units 的合并边界判定",
    "dirty_block": "属解析与噪声治理问题，查 common/mineru_noise.py；改切分代码无效",
    "unknown": "切片文本在原始块中定位失败，可能经过了清洗或改写，需人工核对",
}


def _normalize(text):
    """规范化文本用于比对：去掉空白与常见排版差异。

    切分器会做去标签、合并空白等处理，原文与切片不会逐字相同，
    因此比对前必须归一化，否则全部落到「无法定位」。
    """
    # 去掉所有空白字符
    text = re.sub(r"\s+", "", text or "")
    # 去掉位置标签残留
    text = re.sub(r"@@[\d\-.\t]+##", "", text)
    # 去掉 markdown 强调与常见标记
    text = re.sub(r"[*_`]", "", text)
    # 返回规范化结果
    return text


def _looks_dirty(block):
    """判断原始块本身是否属于脏数据：页眉页脚、页码、目录条目。"""
    # 块类型本身就标明是页眉页脚或页码时直接判定
    if str(block.get("type") or "") in ("page_number", "header", "footer", "aside_text"):
        return True
    # 取块文本做内容判断
    text = (block.get("text") or "").strip()
    # 空块视为脏块
    if not text:
        return True
    # 纯数字是页码
    if re.fullmatch(r"[\d\s\-—]+", text):
        return True
    # 目录条目：以编号开头且以页码数字结尾
    if re.search(r"\s\d{1,4}$", text) and len(text) < 80:
        return True
    # 其余视为正常内容块
    return False


def build_block_index(blocks):
    """把原始块预处理成便于比对的索引，避免逐次重复规范化。"""
    # 逐块生成规范化文本与脏块标记
    return [
        {
            "i": i,  # 原始块序号
            "type": block.get("type", ""),  # 块类型
            "page_idx": block.get("page_idx"),  # 所在页
            "norm": _normalize(block.get("text") or ""),  # 规范化文本
            "dirty": _looks_dirty(block),  # 是否为脏块
            "raw": (block.get("text") or "")[:80],  # 原文片段，用于报告展示
        }
        for i, block in enumerate(blocks)
    ]


def attribute_chunk_tail(chunk_body, index, probe_len=24):
    """判断切片末尾在原始块中的位置，据此归因。

    只看末尾是因为绝大多数边界类问题（截断、列表拆散、标题分离）
    都体现在切片的结束位置上。
    """
    # 规范化切片正文
    norm = _normalize(chunk_body)
    # 内容过短时定位不可靠，直接放弃而不是给出可能错误的结论
    if len(norm) < 8:
        return "unknown", None
    # 取末尾片段作为探针
    probe = norm[-probe_len:] if len(norm) > probe_len else norm

    # 先找以该探针结尾的原始块：说明切片正好在块边界结束
    for b in index:
        # 块文本以探针结尾即边界重合
        if b["norm"] and b["norm"].endswith(probe):
            # 脏块优先归因为解析问题，因为改切分代码修不好
            return ("dirty_block" if b["dirty"] else "block_boundary"), b

    # 再找包含该探针但不以其结尾的块：说明切片在块内部被切开
    for b in index:
        # 包含但不结尾即为块内切分
        if b["norm"] and probe in b["norm"]:
            # 同样先判脏块
            return ("dirty_block" if b["dirty"] else "inside_block"), b

    # 都没命中说明文本经过了清洗或改写，无法定位
    return "unknown", None


def attribute_findings(findings, blocks, records):
    """为每条问题补充归因信息，返回带 attribution 字段的新列表。

    只对与切分边界相关的问题归因；乱码、markdown 残留这类与边界无关的
    问题归因没有意义，强行标注反而误导。
    """
    # 与切分边界相关的检测器，只有它们值得做边界归因
    boundary_detectors = {
        "truncated_sentence", "list_split", "heading_tail",
        "table_split", "caption_orphan", "undersized_chunk", "heading_only",
    }
    # 预处理原始块索引
    index = build_block_index(blocks)
    # 按序号索引切片，便于取正文
    by_index = {r["index"]: r for r in records}
    # 逐条补充归因
    out = []
    # 遍历全部问题
    for f in findings:
        # 转成可变字典
        item = dict(f)
        # 非边界类问题不做归因
        if item.get("detector") not in boundary_detectors:
            out.append(item)
            continue
        # 取对应切片
        rec = by_index.get(item.get("chunk_index"))
        # 切片缺失时无法归因
        if rec is None:
            out.append(item)
            continue
        # 剥离面包屑后归因，面包屑是切分器加的、原始块里没有
        from .detectors import strip_breadcrumb
        # 执行归因
        verdict, block = attribute_chunk_tail(strip_breadcrumb(rec), index)
        # 写入结论与建议
        item["attribution"] = verdict
        item["attribution_label"] = ATTRIBUTION_LABELS[verdict]
        item["attribution_advice"] = ATTRIBUTION_ADVICE[verdict]
        # 命中原始块时附上它，便于人工核对
        if block is not None:
            item["source_block"] = {
                "index": block["i"],  # 原始块序号
                "type": block["type"],  # 块类型
                "page_idx": block["page_idx"],  # 所在页
                "text": block["raw"],  # 原文片段
            }
        # 收入结果
        out.append(item)
    # 返回带归因的问题列表
    return out


def summarize_attribution(findings):
    """按归因结论汇总，回答「这些问题里有多少真该改切分代码」。"""
    # 统计各归因结论的数量
    counts = {}
    # 逐条累计
    for f in findings:
        # 未归因的条目单独归类
        key = f.get("attribution") or "not_applicable"
        # 累加
        counts[key] = counts.get(key, 0) + 1
    # 返回统计结果
    return counts
