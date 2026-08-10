"""生成 Markdown 评估报告。

面向两类读者，因此格式与内容都为它们优化：
  - 人：快速看清哪类问题最多、集中在哪些文件；
  - 编码助手：拿到足够上下文直接定位并修改 ragflow 的切分代码，
    因此每类问题都附上可能相关的源码位置与可本地执行的复现命令。
"""

from collections import defaultdict  # 导入 defaultdict 按类型与样本归组

from .evaluate import active_findings  # 导入有效问题过滤，跳过人工判定的误报
from .paths import REPORT_DIR  # 导入报告目录常量

# 检测器的中文名、严重度与可能相关的 ragflow 源码位置。
# 源码位置是给编码助手的定位线索，不保证唯一，但能大幅缩小排查范围。
DETECTOR_INFO = {
    "truncated_sentence": {
        "cn": "句子截断",
        "sev": "high",
        "desc": "一句话被从中间切开，后半句落到了下一个切片。直接损害召回与阅读。",
        "code": [
            "rag/app/mineru_chunker.py::_merge_text_units（决定何时封存一个 chunk）",
            "rag/app/mineru_chunker.py::_split_oversized_text（超长段落的句切逻辑）",
            "rag/app/mineru_chunker.py::_split_sentences / _split_clauses（句子边界识别）",
        ],
    },
    "undersized_chunk": {
        "cn": "碎片块",
        "sev": "medium",
        "desc": "切片正文过短，不构成完整语义单元，多为排版碎片被单独成块。",
        "code": [
            "rag/app/mineru_chunker.py::_merge_text_units（相邻短段落未合并）",
            "rag/app/mineru_chunker.py::chunk_mineru_blocks（噪声块过滤入口）",
        ],
    },
    "duplicate_content": {
        "cn": "内容重复",
        "sev": "medium",
        "desc": "相邻切片正文高度重叠，浪费索引空间并稀释召回结果。",
        "code": [
            "rag/app/mineru_chunker.py::_merge_text_units（overlap 逻辑）",
            "rag/app/mineru_chunker.py::_tail_sentences（重叠取材）",
        ],
    },
    "markdown_residue": {
        "cn": "Markdown 残留",
        "sev": "medium",
        "desc": "正文残留 ** __ 或 strong/em 标签，会被当作正文字符索引，污染检索。",
        "code": [
            "rag/app/mineru_chunker.py::_strip_md_emphasis（强调标记清洗）",
            "rag/app/mineru_chunker.py::format_block_text（块文本格式化入口）",
            "rag/app/mineru_chunker.py::_clean_slide_text（PPTX 路径已清洗，可参照）",
        ],
    },
    "missing_breadcrumb": {
        "cn": "缺面包屑",
        "sev": "low",
        "desc": "正文切片没有标题路径，检索时失去层级上下文。",
        "code": [
            "rag/app/mineru_chunker.py::_breadcrumb_of（面包屑生成）",
            "rag/app/mineru_chunker.py::_infer_heading_level / _promotable_heading（标题识别）",
        ],
    },
    "noise_residue": {
        "cn": "噪声残留",
        "sev": "high",
        "desc": "页眉页脚或纯页码进入正文切片，直接污染召回结果。",
        "code": [
            "common/mineru_noise.py::filter_mineru_noise_blocks（噪声过滤主逻辑）",
            "rag/app/mineru_chunker.py::_drop_mineru_docx_toc_residue（DOCX 目录治理）",
        ],
    },
    "broken_table": {
        "cn": "表格破损",
        "sev": "high",
        "desc": "表格 HTML 标签不配对或缺少数据单元格，无法正确渲染与检索。",
        "code": [
            "rag/app/mineru_chunker.py::_emit_table_chunks（表格切分与表头重复）",
            "rag/app/mineru_chunker.py::_parse_table_grid / _rows_to_html（表格结构解析）",
        ],
    },
    "heading_only": {
        "cn": "标题孤块",
        "sev": "medium",
        "desc": "切片只有标题没有正文，入库后召回价值为零。",
        "code": ["rag/app/mineru_chunker.py::_merge_text_units（标题未与后续正文合并）"],
    },
    "garbled_text": {
        "cn": "乱码",
        "sev": "high",
        "desc": "正文疑似 OCR 或艺术字识别失败产生乱码。",
        "code": [
            "rag/app/mineru_chunker.py::_looks_garbled（乱码判定）",
            "rag/app/mineru_chunker.py::_ocr_fallback（OCR 兜底）",
        ],
    },
    "empty_image": {
        "cn": "图片空壳",
        "sev": "medium",
        "desc": "图片切片没有任何描述文字，无法被文本检索命中。",
        "code": [
            "rag/app/mineru_chunker.py::_emit_image_chunks（图片切片生成）",
            "rag/app/mineru_chunker.py::_describe_image（VLM 图片描述）",
        ],
    },
    "table_split": {
        "cn": "表格被拆散",
        "sev": "high",
        "desc": "表体切片没有表头，单独看不知道每列是什么。生产有「长表按行组切分并重复表头」的机制，命中说明该机制未生效。",
        "code": [
            "rag/app/mineru_chunker.py::_emit_table_chunks（行组切分与表头重复）",
            "rag/app/mineru_chunker.py::_detect_header_rows（表头行识别）",
        ],
    },
    "dangling_reference": {
        "cn": "指代缺失上下文",
        "sev": "medium",
        "desc": "切片以「其中／此外／上图／该 X」开头，被指代对象在前一个切片。形式完全合法，但单独被检索出来时读者无从理解。",
        "code": [
            "rag/app/mineru_chunker.py::_merge_text_units（相邻段落未合并导致指代与先行词分离）",
            "rag/app/mineru_chunker.py（可考虑为这类切片补充前文摘要或上下文窗口）",
        ],
    },
    "list_split": {
        "cn": "列表被拆散",
        "sev": "medium",
        "desc": "编号项跨切片断开，任何一片单独看都是不完整的枚举。",
        "code": ["rag/app/mineru_chunker.py::_merge_text_units（列表项属独立原始块，默认模式下各自成块）"],
    },
    "heading_tail": {
        "cn": "标题与正文分离",
        "sev": "medium",
        "desc": "标题落在切片末尾，其正文在下一切片。与「标题孤块」不同，这里标题被吸附在上一块结尾，更隐蔽。",
        "code": ["rag/app/mineru_chunker.py::_merge_text_units（标题应与其后正文合并而非留在上一块）"],
    },
    "caption_orphan": {
        "cn": "图注公式脱离上下文",
        "sev": "medium",
        "desc": "图表注或公式独立成块、缺少讲解正文。单独检索无价值，也使被引用对象失去说明。",
        "code": [
            "rag/app/mineru_chunker.py::_emit_image_chunks（图片与图注的关联）",
            "rag/app/mineru_chunker.py::_merge_text_units（图注应与讲解正文合并）",
        ],
    },
    "missing_position": {
        "cn": "缺定位",
        "sev": "low",
        "desc": "切片既无坐标也无页码，前端无法跳转与高亮。",
        "code": [
            "rag/app/mineru_chunker.py::_block_tag / _block_positions（位置标签生成）",
            "rag/app/mineru_chunker.py::_apply_slide_position（Office 路径的位置补全）",
        ],
    },
}

# 严重度的中文标签与排序权重，权重决定问题在报告中的先后
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
SEVERITY_CN = {"high": "高", "medium": "中", "low": "低"}


def _detector_info(name):
    """取检测器的说明信息，未登记时给出兜底结构。"""
    # 已登记则直接返回
    if name in DETECTOR_INFO:
        return DETECTOR_INFO[name]
    # 未登记的检测器仍应出现在报告中，只是缺少定位线索
    return {"cn": name, "sev": "medium", "desc": "", "code": []}


def _lookup_chunk_text(chunks_by_case, case_id, index):
    """从文本快照中取出指定切片的全文，取不到时返回空串。"""
    # 未提供快照时无从查找
    if not chunks_by_case:
        return ""
    # 取该样本的切片列表
    chunks = chunks_by_case.get(case_id) or []
    # 按序号查找
    for c in chunks:
        if c.get("index") == index:
            return c.get("content", "")
    # 未找到
    return ""


def build_markdown(report, chunks_by_case=None, max_full_cases=2):
    """把评估结果渲染成 Markdown 报告。

    max_full_cases 控制每类问题展示多少个「完整案例」——带完整切片正文，
    供编码助手直接理解上下文；其余条目只列位置与证据，避免报告过长。
    """
    # 逐行累积
    L = []
    # 取运行参数与代码指纹，它们决定这份报告对应什么代码状态
    cfg = report.get("config") or {}
    code = report.get("code") or {}

    # ---------- 报告头 ----------
    L.append("# chunk-lab 切分评估报告")
    L.append("")
    L.append(f"- **轮次**：`{report.get('run_id', '-')}`"
             + (f" · {report['label']}" if report.get("label") else ""))
    L.append(f"- **生成时间**：{(report.get('generated_at') or '').replace('T', ' ')}")
    L.append(f"- **切分参数**：chunk_token_num={cfg.get('chunk_token_num', '?')}，"
             f"children_delimiter={cfg.get('children_delimiter', '')!r}")
    # 代码指纹是把报告与代码状态对应起来的唯一依据
    L.append(f"- **被测代码指纹**：`{code.get('hash', '?')}`"
             + (f"（git {code.get('git_commit')}）" if code.get("git_commit") else "")
             + ("　⚠️ 含未提交改动" if code.get("git_dirty") else ""))
    L.append(f"- **规模**：{report.get('case_count', 0)} 个语料 / "
             f"{report.get('chunk_total', 0)} 个切片 / **{report.get('finding_total', 0)} 条问题**")
    L.append("")

    # ---------- 复现说明 ----------
    L.append("## 如何本地复现")
    L.append("")
    L.append("```bash")
    L.append("cd /Users/jialei/Desktop/RagFlow/chunk-lab")
    L.append("")
    L.append("# 复现整轮评估（约十余秒，不需要启动 ragflow，也不连数据库）")
    L.append(f"./run.sh eval --chunk-token-num {cfg.get('chunk_token_num', 512)}")
    L.append("")
    L.append("# 查看某个样本中某一类问题的全部切片（含完整正文与前后文）")
    L.append("./run.sh inspect <样本ID> --detector <检测器名>")
    L.append("")
    L.append("# 查看某个具体切片的完整内容及其相邻切片")
    L.append("./run.sh inspect <样本ID> --chunk <切片序号>")
    L.append("```")
    L.append("")
    L.append("> 改完 `ragflow/rag/app/mineru_chunker.py` 后重新执行 `./run.sh eval --compare`，"
             "即可看到每类问题的增减，以及是否连带影响了其它样本。")
    L.append("")

    # ---------- 问题概览 ----------
    L.append("## 问题概览")
    L.append("")
    L.append("| 问题类型 | 严重度 | 数量 | 涉及样本 |")
    L.append("|---|---|---:|---|")

    # 按检测器归组全部问题
    by_det = defaultdict(list)
    # 逐样本逐条收集，并把样本信息附到每条上
    for case in report.get("cases", []):
        for f in active_findings(case):  # 已判定为误报的条目不进报告
            by_det[f["detector"]].append({**f, "_case": case})

    # 按严重度与数量排序，最该先修的排前面
    ordered = sorted(
        by_det.items(),
        key=lambda kv: (SEVERITY_ORDER.get(_detector_info(kv[0])["sev"], 3), -len(kv[1])),
    )
    # 概览表逐行
    for name, items in ordered:
        info = _detector_info(name)
        # 统计涉及的样本数
        cases = sorted({i["_case"]["case_id"] for i in items})
        L.append(f"| {info['cn']} | {SEVERITY_CN.get(info['sev'], '中')} | {len(items)} | "
                 f"{len(cases)} 个：{', '.join(cases[:4])}{' 等' if len(cases) > 4 else ''} |")
    L.append("")

    # ---------- 逐类问题明细 ----------
    L.append("## 问题明细")
    L.append("")

    # 逐个检测器输出
    for name, items in ordered:
        info = _detector_info(name)
        L.append(f"### {info['cn']}（{len(items)} 条 · 严重度 {SEVERITY_CN.get(info['sev'], '中')}）")
        L.append("")
        # 问题说明，让阅读者不必回查检测器实现
        if info["desc"]:
            L.append(f"**症状**：{info['desc']}")
            L.append("")
        # 可能相关的源码位置，这是给编码助手的定位线索
        if info["code"]:
            L.append("**可能相关的代码**：")
            for c in info["code"]:
                L.append(f"- `{c}`")
            L.append("")

        # 按样本归组，便于按文件逐个修复
        by_case = defaultdict(list)
        for i in items:
            by_case[i["_case"]["case_id"]].append(i)

        # 逐样本输出，数量多的排前面
        for case_id, group in sorted(by_case.items(), key=lambda kv: -len(kv[1])):
            case = group[0]["_case"]
            L.append(f"#### `{case_id}` — {case.get('filename', '')}（{len(group)} 条）")
            L.append("")
            L.append(f"复现：`./run.sh inspect {case_id} --detector {name}`")
            L.append("")
            # 逐条列出，保证「都列举出来」
            for f in sorted(group, key=lambda x: x.get("chunk_index", 0)):
                idx = f.get("chunk_index", -1)
                L.append(f"- **#{idx}**　{f.get('message', '')}")
                # 证据是判断真伪的关键，用代码块保留原始空白与换行
                if f.get("evidence"):
                    ev = f["evidence"].replace("\n", " ⏎ ")
                    L.append(f"  - 证据：`{ev}`")
            L.append("")

        # 每类给出若干完整案例，供编码助手理解完整上下文
        full = items[:max_full_cases]
        if full:
            L.append("<details><summary>完整案例（含切片全文）</summary>")
            L.append("")
            for f in full:
                case = f["_case"]
                idx = f.get("chunk_index")
                L.append(f"**`{case['case_id']}` #{idx}**　{f.get('message', '')}")
                L.append("")
                L.append("```text")
                # 从文本快照取该切片全文；快照缺失时退回证据片段
                content = _lookup_chunk_text(chunks_by_case, case["case_id"], idx) or f.get("evidence", "")
                # 全文可能很长，截断到合理长度避免报告膨胀
                L.append(content[:1200] + ("\n…（已截断）" if len(content) > 1200 else ""))
                L.append("```")
                L.append("")
            L.append("</details>")
            L.append("")

    # ---------- 逐样本汇总 ----------
    L.append("## 逐样本汇总")
    L.append("")
    L.append("| 样本 | 文件 | 原始块 | 切片 | 问题 | 分布 |")
    L.append("|---|---|---:|---:|---:|---|")
    # 按问题数降序，问题多的文件优先处理
    for case in sorted(report.get("cases", []), key=lambda c: -c.get("finding_count", 0)):
        # 切分失败的样本单独标注
        if case.get("error"):
            L.append(f"| `{case['case_id']}` | {case.get('filename', '')} | - | - | 切分失败 | {case['error']} |")
            continue
        # 该样本的问题分布
        dist = "，".join(f"{_detector_info(k)['cn']} {v}"
                         for k, v in sorted((case.get("by_detector") or {}).items(), key=lambda x: -x[1]))
        L.append(f"| `{case['case_id']}` | {case.get('filename', '')} | {case.get('block_count', 0)} | "
                 f"{case.get('chunk_count', 0)} | {case.get('finding_count', 0)} | {dist or '—'} |")
    L.append("")

    # 合并为最终文本
    return "\n".join(L)


def save_markdown(report, chunks_by_case=None, name=None):
    """把 Markdown 报告写入 reports/ 目录，返回文件路径。"""
    # 确保目录存在
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    # 默认以轮次标识命名，便于与历史轮次对应
    name = name or f"{report.get('run_id', 'report')}.md"
    # 目标路径
    path = REPORT_DIR / name
    # 写出报告
    path.write_text(build_markdown(report, chunks_by_case=chunks_by_case), encoding="utf-8")
    # 返回路径供调用方提示
    return path
