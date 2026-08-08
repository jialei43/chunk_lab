"""离线切分驱动：从缓存的 MinerU 产物直接跑 ragflow 的切分逻辑。

这是整个实验室的地基。它绕过 MinerU 解析（慢、贵、几乎不变），只重放
`chunk_mineru_blocks`（快、廉价、每次迭代都在改），把一轮验证从分钟级压到秒级。

刻意不写任何 stub：直接构造真实的 MinerUParser 实例并调用生产函数，
保证离线结论与线上行为零偏差。可行性依据（均已在源码中核对）：
  1. MinerUParser.__init__ 只赋值属性，不做网络调用，可无参构造；
  2. chunk_mineru_blocks 内所有 crop 调用点都有 page_images 守卫，
     不提供页面图像即走"无截图"分支，与生产的 Office 路径行为一致；
  3. tenant_id 传 None 时 get_vision_model/get_chat_model 直接返回 None，
     不会加载 LLM、不会 import api.db、不会连数据库。
"""

import json  # 导入 json 以读取 MinerU 的 content_list.json 产物
import logging  # 导入 logging 记录增强步骤的降级情况
import re  # 导入 re 用于从文件名中剥离扩展名
from pathlib import Path  # 导入 Path 统一处理路径

from .paths import ensure_ragflow_importable  # 导入路径注入函数，必须在 import ragflow 模块之前调用

ensure_ragflow_importable()  # 在导入任何 ragflow 模块之前把仓库根注入 sys.path

from deepdoc.parser.mineru_parser import (MinerUParser, _detect_card_grid_pages,  # noqa: E402
                                          _detect_two_column_text_pages,
                                          _enrich_mineru_from_middle_json)  # 导入真实解析器与产物增强逻辑
from rag.app.mineru_chunker import chunk_mineru_blocks  # noqa: E402  导入被测的切分入口，这是本实验室的核心观测对象
from rag.nlp import rag_tokenizer  # noqa: E402  导入分词器，用于构造与生产一致的 doc 标题字段

# 切分配置的默认值：对齐 naive.py 生产入口的默认 parser_config，使离线结果可与生产对照
DEFAULT_PARSER_CONFIG = {
    "chunk_token_num": 512,  # 单个 chunk 的目标 token 上限，切分逻辑据此决定何时断开
    "delimiter": "\n!?。；！？",  # 段落分隔符，与生产默认值一致
    "layout_recognize": "mineru",  # 声明走 MinerU 路径，供切分逻辑内部的分支判断使用
    "analyze_hyperlink": True,  # 与生产默认值一致
}


def build_child_delimiter_pattern(parser_config):
    """把 parser_config 的 children_delimiter 转成切分器要的正则，复刻 naive.py 的转换。

    这个参数决定合并边界的行为分支，影响极大：
      - 为空时，同标题下的每个原始段落各自独立成块（切得细）；
      - 非空时，只有标题变化才断开，段落在标题内合并成父块（切得粗）。
    离线若不复刻这一步，同一份产物会得到与生产迥异的结果。
    """
    # 读取原始配置值，缺省为空字符串；生产会对其做 unicode_escape 还原以支持 \n 之类的转义写法
    raw = (parser_config.get("children_delimiter") or "")
    # 未配置时直接返回空串，走"每段独立成块"的默认分支，省去无谓的编码往返
    if not raw:
        return ""
    # 复刻生产的转义还原链路：先按 utf-8 编码再按 unicode_escape 解码，使 "\\n" 变成真正的换行
    child_deli = raw.encode("utf-8").decode("unicode_escape").encode("latin1").decode("utf-8")
    # 提取反引号包裹的自定义分隔符，这类词需要整体匹配而非逐字符匹配
    cust_child_deli = re.findall(r"`([^`]+)`", child_deli)
    # 移除反引号片段后，把剩余的单字符分隔符用 | 连接成正则备选
    child_deli = "|".join(re.sub(r"`([^`]+)`", "", child_deli))
    # 存在自定义分隔符时，按长度降序排列以保证长串优先匹配，再转义后并入正则
    if cust_child_deli:
        # 去重并按长度倒序，避免短串抢先匹配掉长串的前缀
        cust_child_deli = sorted(set(cust_child_deli), key=lambda x: -len(x))
        # 逐个转义特殊字符后用 | 连接，防止分隔符内容被当成正则元字符
        cust_child_deli = "|".join(re.escape(t) for t in cust_child_deli if t)
        # 追加到主正则末尾
        child_deli += cust_child_deli
    # 返回最终正则字符串
    return child_deli


def load_content_list(path):
    """读取 MinerU 的 content_list.json，返回原始块列表。"""
    # 统一转成 Path，允许调用方传字符串
    path = Path(path)
    # 以 UTF-8 读取，MinerU 产物中含中文，显式指定编码避免依赖系统默认值
    with path.open("r", encoding="utf-8") as fh:
        # 解析 JSON，MinerU 的 content_list 顶层就是块数组
        data = json.load(fh)
    # 防御：产物格式异常时给出明确报错，而不是让后续代码收到怪异类型
    if not isinstance(data, list):
        raise ValueError(f"content_list.json 顶层应为数组，实际为 {type(data).__name__}：{path}")
    # 返回块列表，交由调用方直接喂给切分器
    return data


def enrich_blocks(blocks, content_list_path):
    """复刻生产 _read_output 中的产物增强步骤。

    ragflow 读完 content_list.json 后会立刻用 middle.json 做三件事：
    双栏 bbox 修复、卡片错位文本归位、跨页续排合并。这些修复只发生在内存里，
    不写回磁盘，因此直接读取磁盘上的 content_list.json 得到的是**增强前**的数据。

    漏掉这一步的后果不是细微偏差：实测某年报 940 块经增强后合并为 934 块，
    离线因此把 6 处线上已经接好的跨页断句报成了「句子截断」——凭空造出缺陷。
    """
    # 样本目录下的 middle.json；导入时保留了 MinerU 原始命名 "{stem}_middle.json"
    matches = list(Path(content_list_path).parent.glob("*_middle.json"))
    # 缺失时保持原样，并回报未增强，供调用方在报告中标注
    if not matches:
        return blocks, None
    # 取第一个匹配项，一个样本目录只会有一份 middle.json
    middle = matches[0]
    # 从文件名还原 stem，生产的加载函数据此拼出待查找的文件名
    stem = middle.name[: -len("_middle.json")]
    # 复刻生产的双栏页识别
    two_column_pages = _detect_two_column_text_pages(blocks)
    # 复刻生产的卡片信息图页识别
    card_grid_pages = _detect_card_grid_pages(blocks)
    # 调用生产的增强逻辑，stem 由文件名还原以便它定位到 middle.json
    try:
        # 执行增强；该函数就地修改 blocks 并返回三类修复计数
        counts = _enrich_mineru_from_middle_json(
            blocks,  # 待增强的块列表，函数内部就地修改
            middle.parent,  # 产物所在目录
            stem,  # 文件名主干，用于拼出 "{stem}_middle.json"
            stem,  # sanitize 后的主干，离线场景与原始主干一致
            logging.getLogger("chunk-lab.enrich"),  # 日志器
            two_column_pages,  # 双栏页集合
            card_grid_pages,  # 卡片页集合
        )
    except Exception as e:
        # 增强失败不应中断评估，但必须回报，避免把降级结果当成正常结果
        logging.warning(f"[chunk-lab] middle.json 增强失败，将使用未增强产物：{e}")
        return blocks, None
    # 返回增强后的块与修复计数
    return blocks, {"two_column": counts[0], "card": counts[1], "continuation": counts[2]}


def build_doc(filename):
    """构造 ES 文档公共字段，与 naive.py 中生产路径的构造方式保持一致。"""
    # docnm_kwd 存原始文件名，切分器会用它判断文档类型（如 DOCX 目录治理只对 .docx 生效）
    doc = {
        "docnm_kwd": filename,  # 原始文件名，含扩展名
        "title_tks": rag_tokenizer.tokenize(re.sub(r"\.[a-zA-Z]+$", "", filename)),  # 去扩展名后分词作为标题词
    }
    # 细粒度分词，生产路径同样会补这一字段，缺失可能影响下游字段完整性检查
    doc["title_sm_tks"] = rag_tokenizer.fine_grained_tokenize(doc["title_tks"])
    # 返回构造好的公共字段字典
    return doc


def _silent_callback(prog=None, msg=""):
    """空进度回调：切分器会周期性上报进度，离线场景不需要输出，但必须可调用。"""
    # 什么都不做，仅满足切分器对 callback 可调用的要求
    return None


def run_offline_chunking(
    content_list_path,
    filename,
    parser_config=None,
    slide_mode=False,
    lang="Chinese",
    eng=False,
):
    """核心入口：读缓存产物 → 跑生产切分逻辑 → 返回原始 chunk 列表。

    参数与生产调用点一一对应，便于逐个复现不同文档类型的行为：
      slide_mode 对应 presentation.py 的 PPTX 分支，其余路径为 False。
    """
    # 读取缓存的 MinerU 块，这一步替代了耗时数分钟的真实解析
    blocks = load_content_list(content_list_path)
    # 复刻生产的 middle.json 增强，缺了它跨页断句会被误报成切分缺陷
    blocks, _enrich_counts = enrich_blocks(blocks, content_list_path)
    # 构造解析器实例：无参构造不触发任何网络请求，仅提供位置标签与截图方法
    parser = MinerUParser()
    # 把块挂到解析器上，与生产中 parse_pdf/parse_file 解析完成后的状态一致
    parser.content_blocks = blocks
    # 构造文档公共字段
    doc = build_doc(filename)
    # 合并切分配置：先铺默认值，再用调用方的覆盖，便于做参数敏感性实验
    config = dict(DEFAULT_PARSER_CONFIG)
    # 仅当调用方确实传了配置时才做覆盖，避免 None 破坏默认值
    if parser_config:
        config.update(parser_config)
    # 由配置推导父子分块正则，与生产 naive.py 的推导方式完全一致
    child_pattern = build_child_delimiter_pattern(config)
    # 调用被测的生产切分函数；tenant_id 显式传 None 以禁用 LLM/VLM 增强，保证离线可复现
    chunks = chunk_mineru_blocks(
        blocks,  # MinerU 原始块
        parser,  # 解析器实例，提供 _line_tag/crop/extract_positions
        doc,  # ES 文档公共字段
        config,  # 切分配置
        eng,  # 是否英文文档
        callback=_silent_callback,  # 空进度回调
        tenant_id=None,  # 关键：置空以跳过所有模型调用，使结果确定可复现
        lang=lang,  # 文档语言，影响乱码检测与提示词
        child_delimiters_pattern=child_pattern,  # 父子分块正则，决定按段落还是按标题划分边界
        slide_mode=slide_mode,  # PPTX 专用的按页切分开关
    )
    # 返回切分器产出的原始 chunk 列表（其中可能含 PIL 图像对象，序列化前需先规范化）
    return chunks


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
