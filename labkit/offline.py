"""离线切分驱动：通过桥接层调用 ragflow 的真实切分入口。

早期版本在这里复刻 naive.chunk 的参数推导，结果反复对不上生产：
children_delimiter 默认值取错、overlapped_percent / table_context_size /
image_context_size 完全没传、middle.json 增强被漏掉。每修一处又冒出新的一处。

现在改为不复刻任何逻辑：调用 ragflow 目录下的 chunklab_bridge，
由它把缓存产物注入生产入口 rag.app.naive.chunk。参数推导、分支选择、
产物增强全部走生产原样，唯一差别是不重新跑 MinerU 解析。
"""

import re  # 导入 re 用于从文件名中剥离扩展名
from pathlib import Path  # 导入 Path 统一处理路径

from . import vlmcache  # 导入 VLM/LLM 结果缓存，避免重放语料时重复调用模型
from .paths import (ensure_ragflow_importable, ensure_ragflow_llm_ready,
                    resolve_tenant_id)  # 导入路径注入、模型环境准备与租户解析

ensure_ragflow_importable()  # 在导入任何 ragflow 模块之前把仓库根注入 sys.path

from chunklab_bridge.bridge import chunk_from_corpus  # noqa: E402  导入桥接层，它负责调用生产切分入口

# 切分配置默认值，逐项对齐生产知识库配置页。
# 字段名取自 ragflow 的权威清单 api/utils/kb_runtime_config.py，
# 默认值取自使用者提供的实际知识库配置截图，而非前端表单预设——
# 表单预设只是新建知识库时的初值，与既有知识库的实际配置未必相同。
DEFAULT_PARSER_CONFIG = {
    "chunk_token_num": 512,  # 建议文本块大小
    "delimiter": "\n",  # 文本分段标识符；生产实际为 \n，非 "\n!?。；！？"
    "enable_children": False,  # 子块用于检索；关闭时不启用父子分块
    "children_delimiter": "\n",  # 子分隔符；仅 enable_children 为真时生效
    "toc_extraction": False,  # 目录增强；依赖对话模型，离线不生效
    "image_table_context_window": 0,  # 图像与表格上下文窗口
    "overlapped_percent": 0,  # 重叠百分比
    "html4excel": False,  # Excel 转 HTML
    "layout_recognize": "mineru",  # 声明走 MinerU 路径
    # 三个增强项开关，在实验室里**一律默认关闭**，需要时到顶栏「切分配置」里手动勾选。
    #
    # 与生产的取舍不同：生产是一次性入库，多花几分钟换更好的召回是划算的；
    # 而实验室要反复重放同一批语料看切分边界的变化，每张图一次 VLM、每张表一次 LLM，
    # 全语料合计约 3400 次调用——一轮评估的绝大部分时间和费用都耗在这上面，
    # 可它们产出的是图片描述与表格摘要文本，对「段落怎么合并、边界切在哪里」毫无影响。
    #
    # 需要评估图片描述质量或表格摘要效果时再手动开，那是另一类实验。
    "mineru_image_desc_enable": False,   # 图片 VLM 描述
    "mineru_table_summary_enable": False,  # 表格 LLM 摘要
    "mineru_ocr_fallback_enable": False,  # 艺术字兜底重识别
}

# 解析阶段参数：这些值在生成缓存产物时就已固化，离线重放改它们不会有任何效果。
# 列出来是为了在界面上明确标注，避免使用者误以为调了能生效。
PARSE_TIME_KEYS = ("mineru_parse_method", "mineru_lang",
                   "mineru_formula_enable", "mineru_table_enable")


# 按文件扩展名推断切片方法。
# 知识库层面配置的是 naive，但 PPTX 在生产中只能走 presentation——
# naive.chunk 根本不处理 .pptx，会直接抛 NotImplementedError。
# 这是文档级而非知识库级的差异，必须按文件类型分派才与生产一致。
EXT_TO_PARSER_ID = {
    ".pptx": "presentation",  # 演示文稿走 presentation 模块
    ".ppt": "presentation",  # 旧版演示文稿同上
}


def infer_parser_id(filename, override=None):
    """推断该文件在生产中实际使用的切片方法。"""
    # 样本显式指定时以其为准，便于对照不同切片方法
    if override:
        return override
    # 按扩展名查表，未命中则用知识库默认的 naive
    return EXT_TO_PARSER_ID.get(Path(filename).suffix.lower(), "naive")


def build_parser_config(overrides=None):
    """合并默认配置与调用方覆盖项，返回最终的 parser_config。"""
    # 先铺默认值
    config = dict(DEFAULT_PARSER_CONFIG)
    # 再用调用方的覆盖，None 值不参与覆盖以免冲掉默认
    for k, v in (overrides or {}).items():
        # 仅在显式给值时覆盖
        if v is not None:
            config[k] = v

    # 「子块用于检索」关闭时必须清空子分隔符。
    # 生产的 naive.chunk 只看 children_delimiter，不看 enable_children，
    # 开关关着却留着分隔符会误启用父子分块，切分粒度立刻与线上不同。
    if not config.get("enable_children"):
        config["children_delimiter"] = ""

    # 图像与表格上下文窗口在界面上是一个值，后端按两个字段消费，
    # 未显式指定时由该窗口值统一填充，保持与知识库配置一致
    window = config.get("image_table_context_window")
    # 仅在窗口值有效且两个细分字段未被单独指定时填充
    if window is not None:
        config.setdefault("image_context_size", window)
        config.setdefault("table_context_size", window)

    # 返回合并结果
    return config


def resolve_llm_tenant():
    """解析本次切分要借用哪个租户的模型授权，不可用时返回空串。

    返回空串表示以纯切分模式运行：表格摘要、图片描述、艺术字兜底全部降级为空，
    这也是未配置 `tenant_id` 时的默认行为。
    """
    # 本机配置指定的租户
    tenant_id = resolve_tenant_id()
    # 未配置时直接按无模型处理，不去碰 ragflow 的 settings
    if not tenant_id:
        return ""
    # 补齐模型厂商清单；补不上说明 ragflow 配置不完整，同样按无模型处理
    return tenant_id if ensure_ragflow_llm_ready() else ""


def run_offline_chunking(corpus_dir, filename, parser_config=None, slide_mode=False,
                         lang="Chinese", eng=False, backend="hybrid_auto", parser_id=None,
                         pdf_path=None, tenant_id=None):
    """核心入口：用缓存产物驱动生产切分逻辑，返回 chunk 列表。

    slide_mode 不再由外部传入——它由生产的 presentation 模块依据文件类型自行决定，
    外部指定反而会与生产行为不一致，故该参数仅为兼容旧调用而保留。

    tenant_id 决定能否加载 LLM/VLM；显式传空串可强制关闭模型调用，
    传 None 表示按本机配置解析。
    """
    # 语料目录：允许传入目录或目录下的 content_list 文件路径
    corpus_dir = Path(corpus_dir)
    # 传入文件时取其所在目录
    if corpus_dir.is_file():
        corpus_dir = corpus_dir.parent
    # 组装最终切分配置
    config = build_parser_config(parser_config)
    # 解析租户：调用方显式给值（含空串）时以其为准，否则按本机配置解析
    tenant = resolve_llm_tenant() if tenant_id is None else tenant_id
    # 装上 VLM/LLM 结果缓存：图片描述、艺术字兜底、表格摘要三类调用将复用历史结果。
    # 幂等且失败自动降级，因此无条件调用即可；缓存不可用时行为与从前完全一致。
    vlmcache.install()
    # 标记当前样本，使本次写入的缓存键登记到该样本索引下，支持按样本清理与用量统计。
    # 用语料目录名而非文件名：目录名是实验室里样本的唯一标识。
    vlmcache.set_sample(corpus_dir.name)
    # 交给桥接层调用生产入口
    return chunk_from_corpus(
        corpus_dir,  # 语料目录，内含 MinerU 原始命名的产物
        filename,  # 原始文件名，决定生产的类型分派
        parser_config=config,  # 切分配置，推导在生产入口内部完成
        lang=lang,  # 文档语言
        backend=backend,  # 产物所用 backend，影响 _read_output 的查找
        parser_id=infer_parser_id(filename, parser_id),  # 切片方法，须与生产一致
        # 借用该租户的模型授权，切分器据此加载 VLM/LLM；为空则三个增强项全部降级
        tenant_id=tenant or None,
        # 关联了原始 PDF 时渲染页面图像，切分才有真实坐标与截图；
        # 渲染有成本，故仅预览传入，批量评估不传以保持速度
        pdf_path=pdf_path,
    )


def normalize_chunk(chunk, index):
    """把生产 chunk 规范化为可 JSON 序列化的记录。

    字段刻意对齐 /chunk/list 接口的返回结构，使离线快路径与 HTTP 慢路径
    可以共用同一套检测器，不必写两份评审逻辑。
    """
    # 取正文：生产字段名为 content_with_weight，是入库与召回的实际内容
    content = chunk.get("content_with_weight", "") or ""
    # 取父块正文：仅父子分块模式下存在，字段名 mom_with_weight（mom 即母块）
    mom = chunk.get("mom_with_weight", "") or ""
    # 读取图像对象，用于判断该 chunk 是否带截图（图像本身不进 JSON）
    image = chunk.get("image")
    # 组装规范化记录
    record = {
        "index": index,  # 该 chunk 在文档内的顺序号，便于定位与前后文比对
        "content": content,  # 正文全文，不截断，检测器需要完整内容判断截断/重复
        "content_len": len(content),  # 正文字符数，长度类检测器的基础指标
        "doc_type_kwd": chunk.get("doc_type_kwd", ""),  # 块类型（text/table/image），分类统计用
        "is_child": bool(mom),  # 是否为父子模式下的子块，检测器据此选择评判口径
        "mom_content": mom,  # 父块全文，子块的上下文来源；非父子模式为空串
        "important_kwd": list(chunk.get("important_kwd", []) or []),  # 标题面包屑加权词，用于检测面包屑丢失
        "question_kwd": list(chunk.get("question_kwd", []) or []),  # 关联问题词，部分路径会写入
        "page_num_int": list(chunk.get("page_num_int", []) or []),  # 页码列表，预览定位依赖它
        "position_int": [list(p) for p in (chunk.get("position_int", []) or [])],  # 坐标列表，转 list 以便序列化
        "top_int": list(chunk.get("top_int", []) or []),  # 纵向排序键，预览顺序依赖它
        "has_image": image is not None,  # 是否带截图，图片类检测器的判据之一
        "image_size": list(image.size) if image is not None else None,  # 截图尺寸，异常尺寸可提示裁剪错误
    }
    # 返回规范化记录
    return record


def normalize_chunks(chunks):
    """批量规范化，返回可直接写入 JSON 的记录列表。"""
    # 逐个规范化并带上顺序号，顺序号来自枚举而非 chunk 内部字段，保证连续
    return [normalize_chunk(chunk, i) for i, chunk in enumerate(chunks)]


def build_doc(filename):
    """构造 ES 文档公共字段。保留此函数仅为兼容旧调用，桥接路径不再使用。"""
    # 延迟导入分词器，避免不使用该函数时承担加载开销
    from rag.nlp import rag_tokenizer
    # docnm_kwd 存原始文件名
    doc = {
        "docnm_kwd": filename,  # 原始文件名，含扩展名
        "title_tks": rag_tokenizer.tokenize(re.sub(r"\.[a-zA-Z]+$", "", filename)),  # 去扩展名后分词
    }
    # 细粒度分词
    doc["title_sm_tks"] = rag_tokenizer.fine_grained_tokenize(doc["title_tks"])
    # 返回构造好的公共字段字典
    return doc
