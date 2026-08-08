"""规则层检测器：把切分缺陷变成可比较的数字。

这一层的价值不在于发现新问题，而在于让"这次改动到底变好还是变坏"成为
一个可以客观回答的问题。没有它，回归防护和 agent 循环都无从谈起。

设计上有两条贯穿全局的口径约定，来自阶段一的实测发现：
  1. 父子分块模式下返回的全部是子块，子块天然短，绝不能按父块的长度判据评判；
  2. 生产只对表格和图片写 doc_type_kwd，正文块该字段为空，统一归一为 text。
"""

import re  # 导入 re 用于各类文本模式匹配
from dataclasses import dataclass, field  # 导入数据类简化结果结构定义

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
# CJK 字间空格模式：连续多个「单个汉字 + 空格」。
# 门槛定得较高（6 个字以上）：封面标题的艺术字排版本就是这种形态且属正常，
# 短片段一律放行，只有正文中出现长串字间空格才可能是解析异常。
SPACED_CJK_PATTERN = re.compile(r"(?:[一-鿿]\s){6,}[一-鿿]")
# 触发字间空格检测所需的正文长度：标题类短块即便有字间空格也是排版选择，不是缺陷
SPACED_MIN_BODY = 40
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


def detect_size_anomaly(records, case_id, cfg):
    """检测块长异常：超出 token 预算过多，或短到不成一个语义单元。"""
    # 收集命中
    findings = []
    # 暂存本样本的全部碎片块，最后统一决定逐条还是聚合上报
    fragments = []
    # 超长判定的 token 阈值
    over_budget = cfg.chunk_token_num * cfg.oversize_ratio
    # 逐块检查
    for r in records:
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
        # 过短判定：子块与父块采用不同阈值，这是阶段一确立的口径
        floor = cfg.min_child_chars if r.get("is_child") else cfg.min_parent_chars
        # 空正文单独由标题孤块检测器负责，这里只管非空但过短的情况
        if 0 < len(body.strip()) < floor:
            # 先收集碎片块，数量决定最终是逐条上报还是聚合上报
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
            message=f"存在 {len(fragments)} 个碎片块（正文短于 {cfg.min_parent_chars} 字），样例：{sample_idx}",
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
                message=f"正文仅 {len(text)} 字，短于下限 {cfg.min_parent_chars}",
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
    """检测面包屑丢失：正文块没有标题路径，检索时失去层级上下文。"""
    # 收集命中
    findings = []
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
    """检测相邻块内容重复：排除设计内的 overlap 之外的异常复制。"""
    # 收集命中
    findings = []
    # 从第二块开始与前一块比较
    for i in range(1, len(records)):
        # 当前块与前一块
        cur, prev = records[i], records[i - 1]
        # 只比较正文块，表格重复表头是设计行为
        if not _is_text_chunk(cur) or not _is_text_chunk(prev):
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


def detect_spaced_characters(records, case_id, cfg):
    """检测正文中的长串字间空格。

    封面标题「年 度 报 告」这类是艺术字的正常排版，不是缺陷，因此设了两道门槛：
    片段需连续 6 个字以上，且所在块要有足够正文长度。只有长正文里夹着长串
    字间空格，才可能是解析异常而非排版选择。
    """
    # 收集命中
    findings = []
    # 逐块检查
    for r in records:
        # 表格 HTML 中的空格属正常排版，只检查正文块
        if not _is_text_chunk(r):
            continue
        # 取剥离面包屑后的正文
        body = strip_breadcrumb(r)
        # 短块多为封面或标题，其字间空格属排版选择，直接放行
        if len(body.strip()) < SPACED_MIN_BODY:
            continue
        # 在正文中查找长串字间空格片段
        match = SPACED_CJK_PATTERN.search(body)
        # 未命中则该块正常
        if not match:
            continue
        # 记录问题，证据直接给出命中的片段
        findings.append(Finding(
            detector="spaced_characters",  # 检测器名
            severity="medium",  # 影响分词与召回，但常见于排版而非解析错误，不再定为高severity
            case_id=case_id,
            chunk_index=r["index"],
            message="长正文中出现连续字间空格，可能影响分词与召回",
            evidence=match.group(0),  # 证据取命中片段本身
        ))
    # 返回全部命中
    return findings


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
    """
    # 收集命中
    findings = []
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
    for r in records:
        # 只检查正文块，图片块由 empty_image 负责
        if not _is_text_chunk(r):
            continue
        # 取剥离面包屑后的正文
        body = strip_breadcrumb(r).strip()
        # 空正文交由标题孤块检测器
        if not body:
            continue
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
    detect_spaced_characters,  # 汉字间异常空格
    detect_markdown_residue,  # Markdown 标记残留
    detect_table_split,  # 表格被拆散（表头与表体分离）
    detect_dangling_reference,  # 指代缺失上下文
    detect_list_split,  # 列表／编号项被拆散
    detect_heading_tail,  # 标题落在切片末尾，与其正文分离
    detect_caption_orphan,  # 图表注与公式脱离上下文
]


def run_detectors(records, case_id, cfg=None):
    """对一个样本的全部 chunk 跑所有检测器，返回合并后的命中列表。"""
    # 未指定配置时使用默认阈值
    cfg = cfg or DetectorConfig()
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
