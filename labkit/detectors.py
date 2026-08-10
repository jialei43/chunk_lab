"""规则层检测器：把切分缺陷变成可比较的数字。

这一层的价值不在于发现新问题，而在于让"这次改动到底变好还是变坏"成为
一个可以客观回答的问题。没有它，回归防护和 agent 循环都无从谈起。

设计上有两条贯穿全局的口径约定，来自阶段一的实测发现：
  1. 父子分块模式下返回的全部是子块，子块天然短，绝不能按父块的长度判据评判；
  2. 生产只对表格和图片写 doc_type_kwd，正文块该字段为空，统一归一为 text。
"""

import re  # 导入 re 用于各类文本模式匹配
from dataclasses import dataclass, field, replace  # 导入数据类简化结果结构定义，replace 用于按样本派生配置副本

from .paths import ensure_ragflow_importable  # 导入路径注入函数

ensure_ragflow_importable()  # 在导入 ragflow 模块之前注入源码路径

from common.token_utils import num_tokens_from_string  # noqa: E402  复用生产的 token 计数，保证与切分预算同口径
from rag.app.mineru_chunker import _looks_garbled  # noqa: E402  直接复用生产的乱码判定，避免另造一套标准

# 句子终止标点集合：中英文句末符号与常见收尾符号，用于判断正文是否被拦腰截断
SENTENCE_ENDINGS = set("。！？!?…；;：:）)】]》>」』”\"'\n")
# 列表项与编号开头的模式，这类块以编号结尾属正常排版，不应判为截断
# 项目符号字符类需覆盖实际文档中的常见形态，首轮评估因漏掉 ● 造成大量误报
LIST_PREFIX_PATTERN = re.compile(r"^\s*(?:[（(]?\d+[）)\.、]|[一二三四五六七八九十]+[、．.]|[•·▪◦●○◆◇■□▲△★☆※→⇒-])")
# 列表结构的行数下限与末行长度上限：多行且末行很短时视为罗列结构，不按整句判截断
LIST_LIKE_MIN_LINES = 2
LIST_LIKE_MAX_TAIL = 25
# 说明：曾有「字间空格」检测器，检查正文中连续的「单汉字 + 空格」。
# 经使用者判定，这类形态属于正常排版（封面艺术字、字间距样式），
# 不构成切分缺陷，故已移除，不要再加回来。
# Markdown 强调标记残留模式：成对的 ** 或 __，以及 HTML 强调标签
MARKDOWN_RESIDUE_PATTERN = re.compile(r"\*\*[^*\n]+\*\*|__[^_\n]+__|</?(?:strong|em|b|i)>", re.IGNORECASE)
# 纯页码模式：整块只有阿拉伯数字或罗马数字，是典型的页眉页脚残留
PURE_PAGE_NUMBER_PATTERN = re.compile(r"^\s*[-—–]?\s*(?:\d{1,4}|[ivxlcdmIVXLCDM]{1,7})\s*[-—–]?\s*$")
# 显式页码文字模式，如「第 3 页」「共 12 页」「Page 4 of 10」
PAGE_LABEL_PATTERN = re.compile(r"^\s*(?:第\s*\d+\s*页|共\s*\d+\s*页|[Pp]age\s*\d+(?:\s*of\s*\d+)?)\s*$")
# 表格 HTML 的关键标签，用于检查表格块结构是否完整
TABLE_TAG_PATTERN = re.compile(r"</?(?:table|tr|td|th)\b", re.IGNORECASE)


@dataclass
class Finding:
    """一条检测结果。字段刻意保持扁平，便于直接序列化进报告与基线。"""

    detector: str  # 检测器名称，报告按此聚类
    severity: str  # 严重度：high / medium / low
    case_id: str  # 所属语料样本
    chunk_index: int  # 命中的 chunk 序号，便于定位复现
    message: str  # 问题的一句话描述
    evidence: str = ""  # 证据片段，截取相关正文便于人工复核


@dataclass
class DetectorConfig:
    """检测阈值集中管理，便于做参数敏感性实验而不改检测逻辑。"""

    chunk_token_num: int = 512  # 切分时的 token 预算，超长判据以它为基准
    oversize_ratio: float = 1.5  # 超出预算多少倍算超长，留出余量避免正常波动误报
    min_parent_chars: int = 15  # 父块（非子块）正文的最短合理字符数
    min_child_chars: int = 8  # 子块正文的最短合理字符数，子块天然更短故阈值更低
    fragment_aggregate_at: int = 20  # 同一样本碎片块超过该数量时聚合成一条，避免淹没其它信号
    truncation_min_chars: int = 50  # 短于此长度的块不做截断判定，避免标题类短块误报
    duplicate_ratio: float = 0.8  # 相邻块正文重叠达到该比例视为重复
    expect_position: bool = True  # 是否要求 chunk 带位置信息，无 bbox 的样本可关闭
    # 当前样本的原始文件名。有些判据本身就是文件类型相关的——例如 PPTX 走整页成块，
    # 没有标题层级是设计使然而非缺陷——离开文件类型就无法正确判定。
    # 由 run_detectors 按样本注入，检测器只读不写。
    filename: str = ""


def strip_breadcrumb(record):
    """剥离 chunk 正文开头的标题面包屑，返回真正的正文。

    切分器产出的正文形如「面包屑\n正文」，面包屑同时写在 important_kwd 中。
    所有长度与截断类判据都必须基于剥离后的正文，否则面包屑会污染统计。
    """
    # 取出正文全文
    content = record["content"]
    # 取出面包屑列表；它是多级标题，切分器用 " > " 连接后前置到正文
    kws = record.get("important_kwd") or []
    # 无面包屑时正文即全文
    if not kws:
        return content
    # 按切分器的拼接方式还原完整面包屑，多级标题必须整体剥离。
    # 只剥第一级会残留 " > 二级标题"，导致正文首字符变成 ">"，
    # 进而让所有基于正文首尾的判据全部失准。
    head = " > ".join(kws)
    # 正文以完整面包屑开头时整体剥离
    if head and content.startswith(head):
        # 去掉面包屑后再去掉紧随其后的换行与空白
        return content[len(head):].lstrip("\n").lstrip()
    # 回退：拼接格式若有出入，则在首级标题命中时剥掉整个首行
    if kws[0] and content.startswith(kws[0]):
        # 面包屑总是独占一行，剥到第一个换行即可
        nl = content.find("\n")
        # 存在换行说明面包屑行之后还有正文
        if nl >= 0:
            return content[nl + 1:].lstrip()
        # 整块只有面包屑没有正文，交由标题孤块检测器处理
        return ""
    # 前缀完全不匹配说明格式与预期不符，保持原样交由后续判据处理
    return content


def _is_text_chunk(record):
    """判断是否为正文块：生产只对表格和图片写 doc_type_kwd，其余均为正文。"""
    # 类型字段为空即正文块
    return not record.get("doc_type_kwd")


def _looks_structural_ending(body):
    """判断块尾是否属于「本就不带句号」的结构性内容。

    版权页、编号、代码、联系方式这类内容天然不以标点收尾，
    单看末字符会把它们全判成截断。
    """
    # 取末行做形态判断，整块过长时只关心结尾
    tail = body.split("\n")[-1]
    # 出版与著录信息：ISBN、CIP、书号等
    if re.search(r"ISBN|CIP|版本馆|出版社|书号", tail):
        return True
    # 联系方式：电话、邮箱、网址
    if re.search(r"(电话|传真|邮箱|邮编|地址)\s*[:：]|@\w+\.|https?://", tail):
        return True
    # 代码痕迹：赋值、调用、箭头、注释符
    if re.search(r"[=（(]\s*\)|\w\(\)|-->|#\s|\bprint\(|\bdef\b|\breturn\b|\.\w+\(", tail):
        return True
    # 整行以拉丁字母与数字为主（英文标题、拼音、编号），中文标点判据不适用
    latin = sum(1 for ch in tail if ch.isascii() and (ch.isalnum() or ch in " .-,:"))
    if tail and latin / len(tail) > 0.8:
        return True
    # 导航路径：用破折号或箭头连接的层级
    if re.search(r"——|——|›|→|>\s*\S+\s*>", tail):
        return True
    # 其余情况不属于结构性结尾
    return False


def _is_continuation(next_record):
    """判断下一块是否为当前块的直接延续。

    这是判定截断的核心信号：真正的截断意味着后文紧接着被切开的半句。
    若下一块另起标题、另起编号，说明当前块只是自然结束、不带句号而已。
    """
    # 没有下一块说明是文档结尾，不构成截断
    if next_record is None:
        return False
    # 表格与图片块不是文本的延续
    if not _is_text_chunk(next_record):
        return False
    # 取下一块剥离面包屑后的正文开头
    nxt = strip_breadcrumb(next_record).lstrip()
    # 空正文无从判断
    if not nxt:
        return False
    # 下一块以编号或项目符号开头，属另起一条，不是延续
    if LIST_PREFIX_PATTERN.match(nxt):
        return False
    # 首字符是汉字或小写英文字母时才认为文意仍在继续；
    # 大写字母、数字、括号等多为新条目的开头
    first = nxt[0]
    return ("一" <= first <= "鿿") or ("a" <= first <= "z")


def detect_truncated_sentence(records, case_id, cfg):
    """检测正文被拦腰截断。

    判据不只看末字符——那个信号太弱，会把 ISBN、代码、电话这类
    天然不带句号的内容全部误判。真正的截断必须同时满足：
    末尾无终止标点、结尾不是结构性内容、且下一块确实是它的延续。
    """
    # 收集本检测器的全部命中
    findings = []
    # 逐块检查，需要访问相邻块故用索引遍历
    for i, r in enumerate(records):
        # 只检查正文块，表格与图片的结尾形态不适用句子判据
        if not _is_text_chunk(r):
            continue
        # 取剥离面包屑后的正文
        body = strip_breadcrumb(r).rstrip()
        # 过短的块多为标题或短语，按截断判定会大量误报
        if len(body) < cfg.truncation_min_chars:
            continue
        # 末尾已是终止标点则正常
        if body[-1] in SENTENCE_ENDINGS:
            continue
        # 按行拆分，末行形态决定该块是整句还是罗列结构
        lines = body.split("\n")
        # 列表项以编号或项目符号开头，其结尾不带句号属正常排版
        if LIST_PREFIX_PATTERN.match(lines[-1]):
            continue
        # 多行且末行很短时视为罗列结构（如年报的数据条目），整句判据不适用
        if len(lines) >= LIST_LIKE_MIN_LINES and len(lines[-1]) <= LIST_LIKE_MAX_TAIL:
            continue
        # 版权页、编号、代码、联系方式等本就不带句号，不构成截断
        if _looks_structural_ending(body):
            continue
        # 取下一块用于判断文意是否仍在继续
        nxt = records[i + 1] if i + 1 < len(records) else None
        # 下一块的标题路径变了说明进入新章节，当前块是章节结尾而非被截断
        if nxt is not None and (r.get("important_kwd") or []) != (nxt.get("important_kwd") or []):
            continue
        # 下一块不是延续时，当前块只是没带句号，不是被切断
        if not _is_continuation(nxt):
            continue
        # 记录一条截断问题，证据同时给出本块结尾与下块开头，便于人工确认确实被切开
        nxt_head = strip_breadcrumb(nxt).lstrip()[:20]
        findings.append(Finding(
            detector="truncated_sentence",  # 检测器名
            severity="high",  # 截断直接损害召回与阅读，定为高severity
            case_id=case_id,  # 样本标识
            chunk_index=r["index"],  # chunk 序号
            message=f"正文疑似被切断，下一块紧接着继续（末字符 {body[-1]!r}）",  # 问题描述
            evidence="…" + body[-30:] + " ⏎▸ " + nxt_head,  # 证据拼接前后文，可直接判断是否真断
        ))
    # 返回全部命中
    return findings


# 切块器认定「够长可独立成块」的门槛比例，与生产 mineru_chunker.STANDALONE_PARAGRAPH_RATIO 对齐。
# 两者必须同步：这是检测器判定「本该合并却没合并」的唯一依据，口径一旦分叉，
# 检测器会把切块器的正常行为成片报成问题。
#
# 生产已由「固定 200 字」改为「token 数 ≥ chunk_token_num × 比例」——够不够独立，
# 本就该相对于目标块大小衡量（256 预算下 205 token 已占八成，1024 预算下才占两成）。
STANDALONE_RATIO = 0.8


def standalone_threshold(cfg):
    """当前配置下「够长可独立成块」的 token 门槛。"""
    # 与生产同式：目标块大小乘以比例
    return cfg.chunk_token_num * STANDALONE_RATIO


def _is_standalone_body(body, cfg):
    """正文是否已长到足以独立成块，与生产 _is_standalone_unit 同口径。"""
    # 用 token 而非字符计数，才与生产的 unit.tokens 对得上
    return num_tokens_from_string(body) >= standalone_threshold(cfg)


def _same_section(a, b):
    """两个 chunk 是否属于同一个标题小节（面包屑完全相同）。"""
    # important_kwd 即标题路径，逐级相同才算同一小节
    return list(a.get("important_kwd") or []) == list(b.get("important_kwd") or [])


def _unmerged_short_neighbor(records, i, cfg):
    """判断第 i 块是否属于「本该与相邻块合并却没合并」。

    这是对生产切块契约的断言：同一标题路径下，相邻两个都短于独立成块阈值的段落
    必然会被合并；因此产出里若还存在这样一对相邻块，只可能是切块器出了问题
    （实测就靠这条抓到了表题吸附越界、把「概述」孤立成 2 字块的缺陷）。

    子块（父子分块产物）不参与：它们由用户子分隔符切出，不走段落合并那条路径。
    """
    # 当前块
    cur = records[i]
    # 子块另有切分契约，不适用本判据
    if cur.get("is_child"):
        return False
    # 只对正文块判定，表格与图片各有独立成块的理由
    if not _is_text_chunk(cur):
        return False
    # 当前块正文；空块归标题孤块检测器管
    body = strip_breadcrumb(cur).strip()
    if not body or _is_standalone_body(body, cfg):
        return False
    # 检查紧邻的前后两块：中间若隔着表格或图片，它们本就被强制分开，不算未合并
    for j in (i - 1, i + 1):
        # 越界即无此邻居
        if j < 0 or j >= len(records):
            continue
        nb = records[j]
        # 邻居必须同样是正文块、非子块，且与当前块同属一个小节
        if nb.get("is_child") or not _is_text_chunk(nb) or not _same_section(cur, nb):
            continue
        # 取邻居正文
        nb_body = strip_breadcrumb(nb).strip()
        # 邻居也未达独立成块门槛时，这两块本该合并
        if nb_body and not _is_standalone_body(nb_body, cfg):
            return True
    # 没有可合并的邻居，短是文档结构使然
    return False


def detect_size_anomaly(records, case_id, cfg):
    """检测块长异常：超出 token 预算过多，或短到不成一个语义单元。"""
    # 收集命中
    findings = []
    # 暂存本样本的全部碎片块，最后统一决定逐条还是聚合上报
    fragments = []
    # 超长判定的 token 阈值
    over_budget = cfg.chunk_token_num * cfg.oversize_ratio
    # PPTX 走 slide_mode 每页一块：同一幻灯片内的块早已合并成一块，跨幻灯片才是硬边界，
    # 因此"本该合并却各自成块"对 pptx 全是误报（都是不同幻灯片的短块，本就不能合并）。
    # 只豁免碎片合并判定，超长判据仍适用（单张幻灯片也可能超预算），故此处只置标志、不整体 return。
    is_pptx = (cfg.filename or "").lower().endswith(".pptx")
    # 逐块检查；碎片判定要看相邻块，因此带上下标
    for i, r in enumerate(records):
        # 取剥离面包屑后的正文作为长度判据基础
        body = strip_breadcrumb(r)
        # 表格块允许显著超长（长表按行组切分本就可能较大），只对正文块判超长
        if _is_text_chunk(r):
            # 用生产的 token 计数保持与切分预算同口径
            tokens = num_tokens_from_string(body)
            # 超出预算容忍上限时记录
            if tokens > over_budget:
                findings.append(Finding(
                    detector="oversized_chunk",  # 检测器名
                    severity="medium",  # 超长影响召回精度但不损坏内容
                    case_id=case_id,
                    chunk_index=r["index"],
                    message=f"正文 {tokens} token，超出预算 {cfg.chunk_token_num} 的 {cfg.oversize_ratio} 倍",
                    evidence=body[:60] + "…",  # 证据取开头片段用于定位
                ))
        # 碎片判定改为**契约派生**，不再用固定字数下限。
        #
        # 生产切块器的契约是：同一标题路径下，两个相邻段落只要都短于独立成块阈值就合并，
        # 直到 token 预算。因此一个短块合法与否，取决于它「本来能不能合并」：
        #   - 它是所在小节的唯一一块  → 无邻居可合，短是文档结构使然，不是缺陷；
        #   - 它与相邻块同属一个小节且两块都短 → 本该合并却没合并，这才是真缺陷。
        # 固定字数下限表达不了这个区别：实测年报里 79%~85% 的短块属于前者，
        # 按字数报出来全是无解的噪声，真正的合并失败反而淹没在里面。
        if not is_pptx and _unmerged_short_neighbor(records, i, cfg):
            # 先收集碎片块，数量决定最终是逐条上报还是聚合上报；pptx 跨片短块非缺陷，不收集。
            fragments.append((r["index"], body.strip()))
    # 碎片块数量超过聚合阈值时压缩成一条，避免上百条低价值条目淹没高severity问题
    if len(fragments) > cfg.fragment_aggregate_at:
        # 取前若干个序号作为样例，便于按图索骥
        sample_idx = ", ".join(f"#{i}" for i, _ in fragments[:5])
        # 产出一条样本级汇总
        findings.append(Finding(
            detector="undersized_chunk",  # 检测器名
            severity="medium",  # 大面积碎片说明切分粒度整体失当，比零星短块更值得关注
            case_id=case_id,
            chunk_index=fragments[0][0],  # 用首个碎片的序号作为锚点
            message=f"存在 {len(fragments)} 个本该合并却各自成块的短块"
                    f"（同一小节内相邻、且都未达独立成块门槛 {standalone_threshold(cfg):.0f} token），样例：{sample_idx}",
            evidence=" | ".join(text for _, text in fragments[:3]),  # 证据给前三个碎片内容
        ))
    else:
        # 数量不多时逐条上报，保留定位精度
        for idx, text in fragments:
            findings.append(Finding(
                detector="undersized_chunk",  # 检测器名
                severity="low",  # 零星短块通常是排版碎片，危害有限
                case_id=case_id,
                chunk_index=idx,
                message=f"正文仅 {len(text)} 字，同一小节内相邻块也未达独立成块门槛"
                        f"（{standalone_threshold(cfg):.0f} token），两者本该合并",
                evidence=text,  # 过短块直接给全文作证据
            ))
    # 返回全部命中
    return findings


def detect_heading_only(records, case_id, cfg):
    """检测标题孤块：只有面包屑没有正文，这类块入库后召回价值为零。"""
    # 收集命中
    findings = []
    # 逐块检查
    for r in records:
        # 只对正文块判定
        if not _is_text_chunk(r):
            continue
        # 剥离面包屑后若正文为空，说明该块只承载了标题
        if strip_breadcrumb(r).strip():
            continue
        # 记录问题，证据用面包屑本身
        findings.append(Finding(
            detector="heading_only",  # 检测器名
            severity="medium",  # 无正文块会稀释召回结果
            case_id=case_id,
            chunk_index=r["index"],
            message="剥离面包屑后无正文，是纯标题块",
            evidence=" > ".join(r.get("important_kwd") or []),  # 证据给出标题路径
        ))
    # 返回全部命中
    return findings


def detect_broken_table(records, case_id, cfg):
    """检测表格结构破损：标签不配对或缺少数据单元格。"""
    # 收集命中
    findings = []
    # 逐块检查
    for r in records:
        # 只检查表格块
        if r.get("doc_type_kwd") != "table":
            continue
        # 取正文全文，表格 HTML 不剥离面包屑以免破坏标签结构
        content = r["content"]
        # 统计开闭标签数量，用于判断结构是否完整
        opens = len(re.findall(r"<table\b", content, re.IGNORECASE))
        # 统计闭合标签
        closes = len(re.findall(r"</table>", content, re.IGNORECASE))
        # 开闭不配对说明表格被切断或拼接错误
        if opens != closes:
            findings.append(Finding(
                detector="broken_table",  # 检测器名
                severity="high",  # 结构破损会导致表格无法正确渲染与检索
                case_id=case_id,
                chunk_index=r["index"],
                message=f"<table> 标签开闭不配对（开 {opens} 闭 {closes}）",
                evidence=content[:80] + "…",
            ))
            # 已记录结构问题就不再重复判定单元格缺失
            continue
        # 存在表格标签却没有任何数据单元格，说明内容丢失
        if opens > 0 and not re.search(r"<t[dh]\b", content, re.IGNORECASE):
            findings.append(Finding(
                detector="broken_table",  # 检测器名
                severity="high",
                case_id=case_id,
                chunk_index=r["index"],
                message="表格块内没有任何 <td>/<th> 单元格",
                evidence=content[:80] + "…",
            ))
    # 返回全部命中
    return findings


def detect_missing_breadcrumb(records, case_id, cfg):
    """检测面包屑丢失：正文块没有标题路径，检索时失去层级上下文。

    PPTX 例外：它走 slide_mode，严格按幻灯片每页一块，标题只取本页首行短文本，
    目录页与过渡页本来就没有标题——这是切分器的设计而非缺陷，报出来全是误报。
    """
    # 收集命中
    findings = []
    # PPTX 整页成块，无标题页天然没有面包屑，整份样本跳过该判据
    if (cfg.filename or "").lower().endswith(".pptx"):
        return findings
    # 定位第一个带面包屑的 chunk：在它之前的块属于文档标题出现前的封面区，天然没有层级
    first_titled = next((i for i, r in enumerate(records) if r.get("important_kwd")), len(records))
    # 逐块检查，跳过封面区
    for r in records[first_titled:]:
        # 只对正文块判定，表格与图片有各自的上下文机制
        if not _is_text_chunk(r):
            continue
        # important_kwd 非空即带面包屑
        if r.get("important_kwd"):
            continue
        # 记录问题
        findings.append(Finding(
            detector="missing_breadcrumb",  # 检测器名
            severity="low",  # 影响召回质量但不损坏内容
            case_id=case_id,
            chunk_index=r["index"],
            message="正文块缺少标题面包屑，检索时无层级上下文",
            evidence=strip_breadcrumb(r)[:60] + "…",
        ))
    # 再检查相邻块之间的“父级被同级误判挤出”症状：路径非空但少了一层，原有空路径检测看不见。
    for current_position, current in enumerate(records[1:], start=1):  # 从第二块开始逐对比较前后标题路径。
        if current.get("doc_type_kwd") != "table":  # 当前已知高价值症状发生在标题分组下的表格块。
            continue  # 其他类型缺少足够强的相邻归属证据，保守不报。
        previous = records[current_position - 1]  # 读取紧邻的上一切片作为潜在父标题上下文来源。
        if not _is_text_chunk(previous):  # 只有上一正文块能稳定携带该小节的完整标题路径与表题文本。
            continue  # 上一块也是媒体时无法证明路径层级缺失。
        previous_path = previous.get("important_kwd") or []  # 读取上一正文块的标题路径。
        current_path = current.get("important_kwd") or []  # 读取当前表格块的标题路径。
        if len(previous_path) < 2 or not current_path:  # 至少需要共同根标题和一个可能丢失的父标题。
            continue  # 证据不足时沿用既有空路径检测结果。
        if previous_path[0] != current_path[0]:  # 不同顶级区段的相邻块属于正常章节切换。
            continue  # 顶级标题不同不能据此推断父级缺失。
        missing_parent = previous_path[-1]  # 上一路径的末级标题是最可能被同级误判挤出的直接父级。
        if missing_parent in current_path:  # 当前路径已经保留该父级时层级关系完整。
            continue  # 不重复报告正常的父子面包屑。
        nearby_lines = strip_breadcrumb(previous).splitlines() + strip_breadcrumb(current).splitlines()  # 合并相邻正文与表格正文寻找编号表题证据。
        caption_evidence = next((line.strip() for line in nearby_lines if CAPTION_PATTERN.match(line.strip()) and missing_parent in line), "")  # 表题同时提及缺失父标题才证明两块仍属同一语义区段。
        if not caption_evidence:  # 没有显式表题关联时可能只是普通同级章节切换。
            continue  # 保守跳过以避免误判正常标题。
        findings.append(Finding(  # 记录路径部分缺失而非整个面包屑为空的回归症状。
            detector="missing_breadcrumb",  # 复用面包屑检测类别以保持报告和前端契约稳定。
            severity="medium",  # 父级丢失会把表格挂到错误章节，影响检索准确性高于普通空路径。
            case_id=case_id,  # 写入当前语料标识便于回归定位。
            chunk_index=current["index"],  # 定位到实际缺父级的表格切片。
            message=f"面包屑疑似缺少父标题“{missing_parent}”，MinerU 可能把父子标题误标为同级",  # 明确指出根因方向与缺失标题。
            evidence=" > ".join(current_path) + " ｜ " + caption_evidence[:60],  # 同时展示错误路径和表题关联证据。
        ))
    # 返回全部命中
    return findings


def detect_noise_residue(records, case_id, cfg):
    """检测噪声残留：纯页码或页眉页脚文字进入了正文 chunk。"""
    # 收集命中
    findings = []
    # 逐块检查
    for r in records:
        # 只对正文块判定
        if not _is_text_chunk(r):
            continue
        # 取剥离面包屑后的正文并去掉首尾空白
        body = strip_breadcrumb(r).strip()
        # 空正文交由标题孤块检测器处理
        if not body:
            continue
        # 整块是纯页码
        if PURE_PAGE_NUMBER_PATTERN.match(body):
            findings.append(Finding(
                detector="noise_residue",  # 检测器名
                severity="high",  # 纯噪声块会直接污染召回结果
                case_id=case_id,
                chunk_index=r["index"],
                message="整块内容是纯页码，属页眉页脚噪声残留",
                evidence=body,
            ))
            # 已判定则不再重复检查页码文字模式
            continue
        # 整块是「第 X 页」这类页码文字
        if PAGE_LABEL_PATTERN.match(body):
            findings.append(Finding(
                detector="noise_residue",  # 检测器名
                severity="high",
                case_id=case_id,
                chunk_index=r["index"],
                message="整块内容是页码标注文字，属页眉页脚噪声残留",
                evidence=body,
            ))
    # 返回全部命中
    return findings


def detect_garbled(records, case_id, cfg, lang="Chinese"):
    """检测乱码：直接复用生产的判定函数，保证与线上兜底逻辑同标准。"""
    # 收集命中
    findings = []
    # 逐块检查
    for r in records:
        # 只对正文块判定，表格 HTML 含大量标签会干扰乱码判据
        if not _is_text_chunk(r):
            continue
        # 取剥离面包屑后的正文
        body = strip_breadcrumb(r).strip()
        # 过短的文本不足以判断乱码，跳过以降低误报
        if len(body) < cfg.truncation_min_chars:
            continue
        # 调用生产的乱码判定；该函数为内部函数，签名变化时应同步更新此处
        if _looks_garbled(body, lang):
            findings.append(Finding(
                detector="garbled_text",  # 检测器名
                severity="high",  # 乱码内容无检索价值且暴露解析缺陷
                case_id=case_id,
                chunk_index=r["index"],
                message="正文被生产乱码判据命中，疑似 OCR 或艺术字识别失败",
                evidence=body[:60] + "…",
            ))
    # 返回全部命中
    return findings


def detect_missing_position(records, case_id, cfg):
    """检测定位缺失：没有位置信息的 chunk 在前端无法跳转与高亮。"""
    # 收集命中
    findings = []
    # 样本显式声明不含位置信息时整体跳过该检测
    if not cfg.expect_position:
        return findings
    # 逐块检查
    for r in records:
        # 同时缺少坐标与页码才算定位能力完全丧失；Office 块无 bbox 但有页码属已知正常形态
        if r.get("position_int") or r.get("page_num_int"):
            continue
        # 记录问题
        findings.append(Finding(
            detector="missing_position",  # 检测器名
            severity="low",  # 不影响内容正确性，只影响预览体验
            case_id=case_id,
            chunk_index=r["index"],
            message="既无 position_int 也无 page_num_int，前端无法定位该切片",
            evidence=strip_breadcrumb(r)[:60] + "…",
        ))
    # 返回全部命中
    return findings


def detect_empty_image(records, case_id, cfg):
    """检测图片空壳：判定为图片块却没有任何描述文字。"""
    # 收集命中
    findings = []
    # 逐块检查
    for r in records:
        # 只检查图片块
        if r.get("doc_type_kwd") != "image":
            continue
        # 剥离面包屑后仍有文字说明描述存在
        if strip_breadcrumb(r).strip():
            continue
        # 记录问题
        findings.append(Finding(
            detector="empty_image",  # 检测器名
            severity="medium",  # 无描述的图片块无法被文本检索命中
            case_id=case_id,
            chunk_index=r["index"],
            message="图片块没有任何描述文字，无法被文本检索命中",
            evidence=" > ".join(r.get("important_kwd") or []),
        ))
    # 返回全部命中
    return findings


def detect_duplicate_content(records, case_id, cfg):
    """检测相邻块内容重复：排除设计内的 overlap 之外的异常复制。

    只在同一标题小节内判定。面包屑不同的两块正文雷同，是文档自身的格式，
    不是切块器复制出来的（理由见下），报出来全是噪声。
    """
    # 收集命中
    findings = []
    # 从第二块开始与前一块比较
    for i in range(1, len(records)):
        # 当前块与前一块
        cur, prev = records[i], records[i - 1]
        # 只比较正文块，表格重复表头是设计行为
        if not _is_text_chunk(cur) or not _is_text_chunk(prev):
            continue
        # 面包屑不同一律不算重复。
        #
        # 这不是经验规则而是结构性结论：切块器的 overlap 只在同一标题路径、
        # 同一原始段落的相邻分片之间生成（mineru_chunker._merge_text_units 里
        # prev_chunk_last_path == current[0].path and prev_chunk_last_order == current[0].order），
        # 跨标题根本产生不出重复内容。因此跨面包屑的正文雷同只可能来自文档本身——
        # 年报里每个小节标题下都有一句「□适用 √不适用」，那是披露格式，不是缺陷。
        #
        # 实测该轮 18 条命中全部是这一类（如「应收利息 > (1) 应收利息分类」与
        # 「应收利息 > (2) 重要逾期利息」各自的「□适用 √不适用」），排除后归零。
        if not _same_section(cur, prev):
            continue
        # 取两块剥离面包屑后的正文
        a, b = strip_breadcrumb(prev).strip(), strip_breadcrumb(cur).strip()
        # 任一为空则无从比较
        if not a or not b:
            continue
        # 完全包含关系是最强的重复信号，按较短者占较长者的比例衡量
        if a in b or b in a:
            # 计算重叠比例：较短文本长度占较长文本长度之比
            ratio = min(len(a), len(b)) / max(len(a), len(b))
            # 比例达到阈值才算异常重复，低比例可能是正常的短语复现
            if ratio >= cfg.duplicate_ratio:
                findings.append(Finding(
                    detector="duplicate_content",  # 检测器名
                    severity="medium",  # 重复内容浪费索引空间并稀释召回
                    case_id=case_id,
                    chunk_index=cur["index"],
                    message=f"与前一块（#{prev['index']}）内容重复，重叠比例 {ratio:.0%}",
                    evidence=b[:60] + "…",
                ))
    # 返回全部命中
    return findings


# Python dunder 标识符：__name__ / __init__ / __main__ 这类，形式上与 markdown 粗体 __xx__ 无法区分
DUNDER_PATTERN = re.compile(r"^__[a-z][a-z0-9_]*__$", re.IGNORECASE)
# 代码块特征词：命中任意一个即认为该正文含源码，其中的 __xx__ 应按标识符而非 markdown 理解
CODE_HINT_PATTERN = re.compile(r"\b(?:def|class|import|from|return|print|self)\b[\s(.]|=>|::")


def _is_code_dunder(matched, body):
    """判断一处 markdown 强调命中是否其实是源码里的 Python dunder 标识符。

    两个条件满足其一即认定为代码：命中片段本身就是 __xxx__ 形式的标识符，
    或所在正文含明显的源码特征。只要有一条成立就不该按 markdown 残留上报。
    """
    # 命中片段形如 __name__，是 dunder 而不是 markdown 粗体
    if DUNDER_PATTERN.match(matched.strip()):
        return True
    # 正文含 def/class/import/print( 等源码特征，整块按代码理解
    return bool(CODE_HINT_PATTERN.search(body))


def detect_markdown_residue(records, case_id, cfg):
    """检测 Markdown 强调标记残留：** __ 与 strong/em 标签不应进入入库正文。

    PPTX 路径已有清洗逻辑，首轮评估显示 DOCX 路径的正文仍带 ** 包裹。
    这类标记会被当作正文字符索引，污染检索结果。
    """
    # 收集命中
    findings = []
    # 逐块检查
    for r in records:
        # 表格块的 HTML 标签属正常结构，只检查正文块
        if not _is_text_chunk(r):
            continue
        # 取剥离面包屑后的正文
        body = strip_breadcrumb(r)
        # 查找残留标记
        match = MARKDOWN_RESIDUE_PATTERN.search(body)
        # 未命中则该块正常
        if not match:
            continue
        # Python 的 dunder（__name__ / __init__ / __main__）形如 __xx__，会被 __[^_\n]+__
        # 当成 markdown 粗体。课件类 PPTX 里满屏都是代码，这条规则曾因此产出 17 条误报。
        if _is_code_dunder(match.group(0), body):
            continue
        # 记录问题
        findings.append(Finding(
            detector="markdown_residue",  # 检测器名
            severity="medium",  # 不破坏语义但会被当作正文字符索引
            case_id=case_id,
            chunk_index=r["index"],
            message="正文残留 Markdown 强调标记或 HTML 强调标签",
            evidence=match.group(0)[:60],  # 证据取命中片段
        ))
    # 返回全部命中
    return findings


def detect_table_split(records, case_id, cfg):
    """检测表格被拆散：表头与表体落到不同切片。

    只查 HTML 标签配对是不够的——标签闭合完好的表体切片照样可能没有表头，
    单独看根本不知道每列是什么。生产有「长表按行组切分并重复表头」的机制，
    因此没有表头的表格切片说明该机制没生效。
    """
    # 收集命中
    findings = []
    # 逐块检查，需要看前一块故用索引
    for i, r in enumerate(records):
        # 只检查表格块
        if r.get("doc_type_kwd") != "table":
            continue
        # 取正文，表格块保留原始 HTML
        content = r["content"]
        # 无表格标签说明不是真正的表格切片，交由其它检测器处理
        if not re.search(r"<table\b", content, re.IGNORECASE):
            continue
        # 含表头行即为完整表格，符合「重复表头」的预期
        if re.search(r"<th\b", content, re.IGNORECASE):
            continue
        # 首行若是加粗或标题样式的单元格，也视为具备表头
        first_row = re.search(r"<tr\b[^>]*>(.*?)</tr>", content, re.IGNORECASE | re.DOTALL)
        # 首行含 <b>/<strong> 通常是以样式表达的表头
        if first_row and re.search(r"<(b|strong)\b", first_row.group(1), re.IGNORECASE):
            continue
        # 取前一块判断是否为同一张表的延续
        prev = records[i - 1] if i > 0 else None
        # 前一块也是表格且面包屑相同，说明这是被拆开的表体
        continued = (prev is not None and prev.get("doc_type_kwd") == "table"
                     and (prev.get("important_kwd") or []) == (r.get("important_kwd") or []))
        # 记录问题，区分「表体独立成块」与「整表无表头」
        findings.append(Finding(
            detector="table_split",  # 检测器名
            severity="high",  # 无表头的表格切片单独检索时无法理解，危害大
            case_id=case_id,
            chunk_index=r["index"],
            message="表格切片没有表头行" + ("，且紧接前一个表格切片，疑似表体被拆散" if continued else ""),
            evidence=re.sub(r"\s+", " ", content)[:80] + "…",
        ))
    # 返回全部命中
    return findings


# 指代词表：出现在切片开头意味着该切片依赖前文才能读懂。
# 只收对上下文依赖强的词，「这」「它」这类过于常见会造成大量误报。
ANAPHORA_PATTERN = re.compile(
    r"^[\s，。、]*("
    r"如上所述|上述|前述|前文|如前|综上|由此可见|因此|据此|"
    r"该(?:公司|部门|机构|系统|平台|项目|办法|规定|条款|方案|文件|章节|表|图)|"
    r"其中|上表|上图|下表|下图|如下图所示|如上图所示|此外|另外"
    r")")


def detect_dangling_reference(records, case_id, cfg):
    """检测指代缺失上下文：切片开头就用指代词，被指代的对象在前一个切片。

    这类切片单独被检索出来时读者无从知道「该公司」「上述办法」指的是什么，
    是 RAG 场景下最影响可用性的一类问题，但形式上完全合法，
    只看首尾标点的检测器抓不到。
    """
    # 收集命中
    findings = []
    # 预先算出每个块在其所属标题小节内的序号。
    # 「指代词开头」是否真的读不懂，取决于被指代对象落在哪一侧的边界外，
    # 而小节内序号正好把两种情形分开（判据见下面的豁免分支）。
    #
    # 计数**必须把表格与图片一起数进去**：指代对象常常正是同小节里的那张表
    # （「上述投资额仅为主营业务长期资产购建投入…」指的就是紧邻的投资额表格）。
    # 只数正文块会把这类块误算成小节首块，从而漏报真问题——实测漏了 3 条。
    section_seq = {}
    # 逐小节累计出现次数，序号即该块在本小节中的位次
    seen = {}
    # 按文档顺序遍历，保证序号反映真实先后
    for r in records:
        # 面包屑即标题路径，作为小节标识
        key = tuple(r.get("important_kwd") or [])
        # 该小节已出现的块数加一，表格与图片同样占位
        seen[key] = seen.get(key, 0) + 1
        # 记下本块在小节内的位次
        section_seq[r["index"]] = seen[key]
    # 从第二块开始检查，首块没有前文可依赖
    for i, r in enumerate(records):
        # 只检查正文块
        if not _is_text_chunk(r):
            continue
        # 文档首块即便有指代词也无前文可指，跳过
        if i == 0:
            continue
        # 取剥离面包屑后的正文
        body = strip_breadcrumb(r).lstrip()
        # 空正文由标题孤块检测器负责
        if not body:
            continue
        # 匹配开头的指代词
        m = ANAPHORA_PATTERN.match(body)
        # 未命中说明开头不依赖前文
        if not m:
            continue
        # 位于独立标题小节**首块**（含表格图片在内的第一个块）且带面包屑时不算缺失上下文。
        #
        # 首块的指代必然跨小节——同小节没有更靠前的块可指。而跨小节意味着标题已经
        # 切换话题，面包屑本身就是新的上下文锚点：检索命中「上述与合同成本有关的资产…」
        # 时，面包屑「… > 4.合同成本减值」已经说清这段在讲什么，读者不会不知所云。
        # 反过来，小节第二块及以后的指代指向的是**同一小节里更靠前的那个块**，
        # 那才是切分造成的断裂——它可能是被切走的前一段正文（「上图输入框中的输入信息
        # 参见下表。」），也可能是同小节的一张表（「上述投资额…」指紧邻的投资额表格）。
        # 无面包屑的块没有这个锚点，不适用本豁免。
        if section_seq.get(r["index"]) == 1 and (r.get("important_kwd") or []):
            continue
        # 指代对象若在本切片内已出现（如「该公司」后文又写了公司全名），则不算缺失。
        # 用指代词后紧跟的名词做粗判：本块内该名词出现两次以上说明有先行词。
        word = m.group(1)
        # 取指代词中的核心名词部分用于计数
        noun = word[1:] if word.startswith("该") else ""
        # 本块内该名词出现多次说明上下文自足
        if noun and body.count(noun) >= 2:
            continue
        # 记录问题，证据给出开头片段便于人工判断
        findings.append(Finding(
            detector="dangling_reference",  # 检测器名
            severity="medium",  # 内容完整但脱离上下文，检索命中后可读性差
            case_id=case_id,
            chunk_index=r["index"],
            message=f"切片以指代词「{word}」开头，被指代对象在前文，单独检索时读不懂",
            evidence=body[:60] + "…",
        ))
    # 返回全部命中
    return findings


def detect_list_split(records, case_id, cfg):
    """检测列表被拆散：编号项跨切片断开。

    「一、二、三」被拆到两个切片时，任何一片单独看都是不完整的枚举。

    但「同标题下没有合并到一起」本身不构成缺陷——切块器有两个合法的断开理由，
    命中任一条时列表跨块都是契约使然而非缺陷，判据必须先把它们排除掉（见下）。

    PPTX 例外：走 slide_mode 每页一块，跨幻灯片的相邻列表项是切分契约而非缺陷，整份样本跳过。
    """
    # 收集命中
    findings = []
    # PPTX 整页成块，跨幻灯片列表跨块属 slide_mode 契约，直接跳过避免全是误报
    if (cfg.filename or "").lower().endswith(".pptx"):
        return findings
    # 从第二块开始，需要与前一块比较
    for i in range(1, len(records)):
        # 当前块与前一块
        cur, prev = records[i], records[i - 1]
        # 只比较正文块
        if not _is_text_chunk(cur) or not _is_text_chunk(prev):
            continue
        # 面包屑不同说明跨章节，列表本就该断开
        if (cur.get("important_kwd") or []) != (prev.get("important_kwd") or []):
            continue
        # 取两块正文
        cur_body = strip_breadcrumb(cur).strip()
        prev_body = strip_breadcrumb(prev).strip()
        # 任一为空则无从判断
        if not cur_body or not prev_body:
            continue
        # 可合并性前置判断：只有「本来能合并却没合并」才是缺陷。
        #
        # 生产 _merge_text_units 对同一标题下的相邻段落只有两个断开理由：
        #   crossed = _is_standalone_unit(前) or _is_standalone_unit(本)   —— 任一侧达到独立成块门槛
        #   current_tokens + unit.tokens > budget                        —— 合并后超 token 预算
        # 两者都不成立时切块器必然合并，产出里还分着才说明它出了问题。
        #
        # 不加这道判断，长段落组成的编号列表会被成片误报：实测 76 条命中里 64 条属此类，
        # 典型如「（2）存货减值的估计…」478 字接「（3）长期资产减值的估计…」——
        # 两个都是完整的独立语义单元，切块器本来就该让它们各自成块。
        #
        # 两侧都判而不只判前一块：契约里是 or，「短块接长块」同样会断开，
        # 只判前块的话这类仍会误报（实测 12 条）。
        if _is_standalone_body(prev_body, cfg) or _is_standalone_body(cur_body, cfg):
            continue
        # 合并后超出 token 预算时，断开是预算所限而非缺陷。
        # 门槛是预算的八成，因此两侧都未达门槛时 token 和最多约 1.6 倍预算，
        # 这条确实可能单独触发，不能省。
        if num_tokens_from_string(prev_body) + num_tokens_from_string(cur_body) > cfg.chunk_token_num:
            continue
        # 当前块首行与前一块末行都是列表项，说明列表被从中间切开
        cur_first = cur_body.split("\n")[0]
        prev_last = prev_body.split("\n")[-1]
        # 两侧都匹配列表模式才判定
        if not (LIST_PREFIX_PATTERN.match(cur_first) and LIST_PREFIX_PATTERN.match(prev_last)):
            continue
        # 记录问题
        findings.append(Finding(
            detector="list_split",  # 检测器名
            severity="medium",  # 枚举不完整影响理解，但每项本身可读
            case_id=case_id,
            chunk_index=cur["index"],
            message=f"列表被拆散：前一块（#{prev['index']}）末尾与本块开头都是列表项",
            evidence=prev_last[:34] + " ⏎▸ " + cur_first[:34],
        ))
    # 返回全部命中
    return findings


# 标题模式：只认强章节特征。
# 早期版本把「\d+[、．.]」也当标题，结果把「7 月，胡文辉副局长应邀赴…」这类
# 正文和「15．关于…的决定」这类列表项全部误判，故收紧为必须含章节量词。
HEADING_TAIL_PATTERN = re.compile(
    r"^(?:第[一二三四五六七八九十百\d]+[章节编篇部]|"
    r"[一二三四五六七八九十]+[、．.]\s*[^\d]|"
    r"\d+(?:\.\d+)+\s*\S)")
# 标题不应含这些正文特征：出现即说明是句子而非标题
HEADING_EXCLUDE = re.compile(r"[，,；;：:]|月，|年，|日，")
# 目录项模式：正文 + 引导点（或多个空格）+ 页码收尾，如
# 「第八节 财务报告....48」「第八节 财务报告 …… 41」「第八节 财务报告    30」。
# 目录里每一行都长得像标题（「第八节 财务报告」确实是章节名），但它们是**目录项**，
# 其正文本来就在几十页之后，不存在「标题与正文被切开」这回事。
TOC_ENTRY_PATTERN = re.compile(r"(?:[.．·・…]{2,}\s*|\s{2,})\d{1,4}\s*$")


def detect_heading_tail(records, case_id, cfg):
    """检测标题与正文分离：标题落在切片末尾，正文在下一个切片。

    heading_only 只覆盖「整块仅有标题」，覆盖不到这种更隐蔽的情况：
    标题被吸附在上一块结尾，下一块的正文因此失去了标题上下文。
    """
    # 收集命中
    findings = []
    # 逐块检查，需要看下一块
    for i, r in enumerate(records[:-1]):
        # 只检查正文块
        if not _is_text_chunk(r):
            continue
        # 取正文并按行拆分
        body = strip_breadcrumb(r).rstrip()
        # 单行块由 heading_only 负责，这里只看多行块的末行
        lines = [ln for ln in body.split("\n") if ln.strip()]
        # 少于两行无从判断「标题被吸附在末尾」
        if len(lines) < 2:
            continue
        # 末行
        tail = lines[-1].strip()
        # 末行需短且形似标题
        if len(tail) > 30 or not HEADING_TAIL_PATTERN.match(tail):
            continue
        # 末行以句末标点结束说明是正文句子而非标题
        if tail[-1] in SENTENCE_ENDINGS:
            continue
        # 含逗号分号等句内标点的是正文，不是标题
        if HEADING_EXCLUDE.search(tail):
            continue
        # 末行是目录项（带引导点与页码）时不算标题。
        # 目录页每一行都形似标题——「第八节 财务报告」本就是章节名——但它们指向的正文
        # 在几十页之外，本来就不该跟在目录后面，谈不上「被切开」。
        # 实测该检测器 10 条命中里有 9 条是目录页末行「第八节 财务报告....48」这一形态。
        if TOC_ENTRY_PATTERN.search(tail):
            continue
        # 前一行也是同类编号项时，这是列表而非标题，交由 list_split 处理
        if LIST_PREFIX_PATTERN.match(lines[-2].strip()):
            continue
        # 下一块必须有正文，才构成「标题与正文被分开」
        nxt = records[i + 1]
        # 下一块非正文块时不构成该问题
        if not _is_text_chunk(nxt) or not strip_breadcrumb(nxt).strip():
            continue
        # 记录问题
        findings.append(Finding(
            detector="heading_tail",  # 检测器名
            severity="medium",  # 下一块正文失去标题上下文，影响召回准确性
            case_id=case_id,
            chunk_index=r["index"],
            message="标题落在切片末尾，其正文在下一切片，二者被分开",
            evidence=tail + " ⏎▸ " + strip_breadcrumb(nxt).lstrip()[:30],
        ))
    # 返回全部命中
    return findings


# 图表与公式的引用标记，用于判断切片是否只有编号而无讲解
CAPTION_PATTERN = re.compile(r"^\s*(图|表|式|附表|附图)\s*[\d一二三四五六七八九十]+[\s:：.、]?")
# 公式标记：LaTeX 定界符或行内公式痕迹
FORMULA_PATTERN = re.compile(r"\$\$|\\\[|\\begin\{(?:equation|align)")


def detect_caption_orphan(records, case_id, cfg):
    """检测图注/表注/公式脱离上下文：切片只有编号标题，没有讲解正文。

    「图 3-1 系统架构图」单独成块时既检索不到，也解释不了任何东西；
    公式同理，编号与讲解被分开后两边都失去意义。
    """
    # 收集命中
    findings = []
    # 逐块检查
    for record_position, r in enumerate(records):  # 保留下标以检查表题是否残留在紧邻媒体块的上一正文块末尾。
        # 只检查正文块，图片块由 empty_image 负责
        if not _is_text_chunk(r):
            continue
        # 取剥离面包屑后的正文
        body = strip_breadcrumb(r).strip()
        # 空正文交由标题孤块检测器
        if not body:
            continue
        # 检查更隐蔽的形态：表题不是独立短块，而是被短段落合并器粘在上一正文块末尾。
        lines = [line.strip() for line in body.splitlines() if line.strip()]  # 去掉空行后读取正文最后一个语义行。
        tail = lines[-1] if lines else ""  # 末行是潜在图表题，正文主体长度不再影响判断。
        next_record = records[record_position + 1] if record_position + 1 < len(records) else None  # 读取紧邻后继块确认题注对象。
        next_is_media = bool(next_record) and next_record.get("doc_type_kwd") in ("table", "image")  # 只有紧邻表格或图片才构成题注错位。
        current_pages = set(r.get("page_num_int") or [])  # 读取正文块页码以排除目录页与后文媒体恰好相邻的情况。
        next_pages = set((next_record or {}).get("page_num_int") or [])  # 读取后继媒体块页码并保持无页码路径可降级。
        same_page = not current_pages or not next_pages or bool(current_pages & next_pages)  # 双方有页码时必须存在同页交集，无页码时不额外猜测。
        compact_tail = re.sub(r"\s+", "", tail)  # 归一化 MinerU 可能插入的普通空格与全角空白。
        compact_next = re.sub(r"\s+", "", (next_record or {}).get("content", ""))  # 以同一口径归一化媒体块正文。
        caption_left_in_text = bool(CAPTION_PATTERN.match(tail)) and len(tail) <= 80 and next_is_media and same_page and compact_tail not in compact_next  # 表题只在同页正文末尾且未进入媒体块时判为错位。
        if caption_left_in_text:  # 命中用户截图中的“正文 + 表题 / 下一块表格”症状。
            findings.append(Finding(  # 把问题归入既有 caption_orphan 类别以保持报告兼容。
                detector="caption_orphan",  # 检测器名沿用图表注脱离上下文类别。
                severity="medium",  # 表格失去表题会降低独立召回结果的可解释性。
                case_id=case_id,  # 写入当前语料标识。
                chunk_index=r["index"],  # 定位到错误残留表题的上一正文切片。
                message="图表题残留在上一正文块末尾，未合并到紧邻媒体块",  # 描述与 UI 中实际症状一致。
                evidence=tail[:80] + " ⏎▸ " + (next_record or {}).get("content", "")[:30],  # 展示错位边界两侧的文本证据。
            ))
            continue  # 同一表题无需再按“整体短块”规则重复报告。
        # 目录条目形如「附表 1 xxx统计数据 81」，末尾的数字是页码而非内容。
        # 它们属于目录页残留，应由噪声治理处理，误判成孤立图注会掩盖真正的问题。
        if re.search(r"\s\d{1,4}$", body):
            continue
        # 判断是否为孤立的图表注：以编号开头且整体很短
        is_caption = bool(CAPTION_PATTERN.match(body)) and len(body) <= 40
        # 判断是否为孤立公式：含公式标记且几乎没有说明文字
        is_formula = bool(FORMULA_PATTERN.search(body)) and len(re.sub(r"[^一-鿿]", "", body)) < 10
        # 两者都不是则跳过
        if not (is_caption or is_formula):
            continue
        # 记录问题，区分两种形态便于针对性修复
        findings.append(Finding(
            detector="caption_orphan",  # 检测器名
            severity="medium",  # 单独成块无检索价值，且使被引用对象失去说明
            case_id=case_id,
            chunk_index=r["index"],
            message="图表注或公式独立成块，缺少讲解正文" if is_caption else "公式独立成块，缺少讲解正文",
            evidence=body[:60],
        ))
    # 返回全部命中
    return findings


# 全部检测器登记表：新增检测器只需在此追加，运行与报告端无需改动
ALL_DETECTORS = [
    detect_truncated_sentence,  # 句子截断
    detect_size_anomaly,  # 块长异常（超长与过短）
    detect_heading_only,  # 标题孤块
    detect_broken_table,  # 表格结构破损
    detect_missing_breadcrumb,  # 面包屑丢失
    detect_noise_residue,  # 页眉页脚噪声残留
    detect_garbled,  # 乱码
    detect_missing_position,  # 定位信息缺失
    detect_empty_image,  # 图片空壳
    detect_duplicate_content,  # 相邻块重复
    detect_markdown_residue,  # Markdown 标记残留
    detect_table_split,  # 表格被拆散（表头与表体分离）
    detect_dangling_reference,  # 指代缺失上下文
    detect_list_split,  # 列表／编号项被拆散
    detect_heading_tail,  # 标题落在切片末尾，与其正文分离
    detect_caption_orphan,  # 图表注与公式脱离上下文
]


def run_detectors(records, case_id, cfg=None, filename=""):
    """对一个样本的全部 chunk 跑所有检测器，返回合并后的命中列表。

    filename 是该样本的原始文件名，按样本注入到配置副本里供文件类型相关的判据使用。
    """
    # 未指定配置时使用默认阈值
    cfg = cfg or DetectorConfig()
    # 按样本复制一份配置并写入文件名：cfg 由调用方跨样本复用，直接改会把上一个样本的文件名带到下一个
    if filename:
        cfg = replace(cfg, filename=filename)
    # 汇总所有检测器的命中
    findings = []
    # 逐个执行检测器
    for detector in ALL_DETECTORS:
        # 单个检测器异常不应中断整轮评估，因此逐个捕获
        try:
            # 执行并合并结果
            findings.extend(detector(records, case_id, cfg))
        except Exception as e:
            # 把检测器自身的故障也记录为一条 finding，避免静默丢失
            findings.append(Finding(
                detector=detector.__name__,  # 用函数名标识出错的检测器
                severity="high",  # 检测器故障必须被看见
                case_id=case_id,
                chunk_index=-1,  # 无具体 chunk 时用 -1 表示样本级问题
                message=f"检测器执行失败：{type(e).__name__}: {e}",
            ))
    # 返回全部命中
    return findings
