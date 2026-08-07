"""扫描本机 MinerU 产物，供前端挑选导入。

本机产物目录里同一份文档往往有多个副本：每解析一次就多一个带随机后缀的目录。
本模块按「文档名 + backend」归组去重，每组只保留最新的一份，
但同一文档的不同 backend 会各保留一份——它们喂给切分器的块数可能差两成，
是有价值的对照，不能当成重复丢掉。
"""

import json  # 导入 json 读取产物以统计块数
import re  # 导入 re 剥离目录名中的随机后缀
from pathlib import Path  # 导入 Path 处理路径

from .paths import CORPUS_DIR  # 导入语料目录，用于标记哪些产物已导入

# 本机 MinerU 服务的输出根目录
MINERU_RESULT = Path("/Users/jialei/MinerU/result")
# RagFlow 侧的实验产物根目录
RAGFLOW_TMP = Path("/Users/jialei/Desktop/RagFlow/tmp/pdfs")
# MinerU 输出目录名的随机后缀模式，形如 xxx_auto_a1b2c3d4
RANDOM_SUFFIX_PATTERN = re.compile(r"_auto_[0-9a-z_]{6,}$", re.IGNORECASE)
# 文档名末尾的重复副本编号，形如 xxx(1) / xxx(2)
COPY_INDEX_PATTERN = re.compile(r"\((\d+)\)$")
# 依据文件扩展名推断文档大类，供前端分组展示
EXT_TO_KIND = {
    ".pdf": "pdf",  # PDF 文档
    ".pptx": "pptx",  # 演示文稿
    ".ppt": "pptx",  # 旧版演示文稿
    ".docx": "docx",  # Word 文档
    ".doc": "docx",  # 旧版 Word 文档
    ".xlsx": "xlsx",  # 电子表格
    ".xls": "xlsx",  # 旧版电子表格
}


def _normalize_doc_name(stem):
    """把产物目录名还原成文档名：去掉随机后缀与副本编号。"""
    # 先去掉 _auto_xxxx 随机后缀
    name = RANDOM_SUFFIX_PATTERN.sub("", stem)
    # 再去掉结尾的 (1) (2) 这类副本编号
    name = COPY_INDEX_PATTERN.sub("", name).strip()
    # 返回归一化后的文档名
    return name


def _guess_extension(name):
    """从文档名推断原始扩展名，供切分器判断文档类型。"""
    # 文档名本身已带扩展名时直接采用（部分产物名形如 xxx.docx）
    suffix = Path(name).suffix.lower()
    # 命中已知扩展名则直接返回
    if suffix in EXT_TO_KIND:
        return suffix
    # 无法从名称判断时返回空，由调用方按 backend 目录补充推断
    return ""


def _backend_of(json_path):
    """从产物路径推断 backend：MinerU 会按 backend 建一级子目录。"""
    # content_list.json 的父目录名即 backend 标识（hybrid_auto / vlm / office 等）
    return json_path.parent.name


def _imported_keys():
    """读取已导入语料，返回其「文档名 + backend」归组键集合。

    不能用完整路径做匹配：磁盘上同一文档往往有多个随机后缀副本，
    导入时用的那一个未必是扫描时择新保留的那一个，按路径比对会把
    已导入的样本误标成未导入，进而导致重复导入。
    """
    # 收集已导入样本的归组键
    keys = set()
    # 语料目录不存在时视为尚未导入任何样本
    if not CORPUS_DIR.is_dir():
        return keys
    # 遍历每个样本目录
    for case_dir in CORPUS_DIR.iterdir():
        # 描述文件路径
        meta = case_dir / "case.yaml"
        # 跳过缺少描述文件的目录
        if not meta.is_file():
            continue
        # 逐行读取以避免为一个字段引入 yaml 解析开销
        for line in meta.read_text(encoding="utf-8").splitlines():
            # source 字段记录了产物的原始路径
            if line.startswith("source:"):
                # 取冒号后的路径
                source = Path(line.split(":", 1)[1].strip())
                # 按与扫描端完全相同的方式推导归组键，保证两侧口径一致
                keys.add((_normalize_doc_name(source.parent.parent.name), source.parent.name))
                # 找到即可停止扫描该文件
                break
    # 返回全部已导入的归组键
    return keys


def scan_products(roots=None):
    """扫描全部产物，按「文档名 + backend」去重后返回候选列表。"""
    # 未指定时扫描两个默认根目录
    roots = roots or [MINERU_RESULT, RAGFLOW_TMP]
    # 已导入样本的归组键集合，用于标记状态
    imported = _imported_keys()
    # 以「文档名 + backend」为键归组，值为该组中最新的产物
    groups = {}
    # 逐个根目录扫描
    for root in roots:
        # 根目录不存在时跳过，避免因环境差异中断
        if not Path(root).is_dir():
            continue
        # 递归查找全部 content_list.json
        for json_path in Path(root).rglob("*_content_list.json"):
            # 取产物所在的解析目录名，它包含文档名与随机后缀
            parse_dir = json_path.parent.parent.name
            # 归一化得到文档名
            doc_name = _normalize_doc_name(parse_dir)
            # 文档名为空说明目录结构不符合预期，跳过
            if not doc_name:
                continue
            # 识别 backend
            backend = _backend_of(json_path)
            # 归组键：同一文档的不同 backend 分别保留
            key = (doc_name, backend)
            # 取修改时间用于组内择新
            mtime = json_path.stat().st_mtime
            # 组内已有更新的产物时跳过当前项
            if key in groups and groups[key]["mtime"] >= mtime:
                continue
            # 记录该组的当前最优产物
            groups[key] = {
                "doc_name": doc_name,  # 归一化后的文档名
                "backend": backend,  # 解析所用 backend
                "path": str(json_path),  # 产物绝对路径
                "mtime": mtime,  # 修改时间，用于组内择新
            }

    # 把归组结果整理成前端需要的列表
    items = []
    # 逐组补充块数与状态信息
    for info in groups.values():
        # 读取块数；产物损坏时记为 -1 并保留条目，便于用户看到异常
        try:
            # 打开并解析产物
            with open(info["path"], "r", encoding="utf-8") as fh:
                # 顶层数组长度即原始块数
                block_count = len(json.load(fh))
        except Exception:
            # 解析失败用 -1 标记
            block_count = -1
        # 推断扩展名，用于组装切分器需要的文件名
        ext = _guess_extension(info["doc_name"])
        # office backend 且无法从名称判断扩展名时，默认按 pptx 处理并由用户在前端修正
        if not ext:
            # office 目录说明是 Office 文档，其余按 PDF 处理
            ext = ".pptx" if info["backend"] == "office" else ".pdf"
        # 组装候选项
        items.append({
            "doc_name": info["doc_name"],  # 文档名
            "backend": info["backend"],  # backend 标识
            "path": info["path"],  # 产物路径
            "block_count": block_count,  # 原始块数
            "filename": info["doc_name"] if _guess_extension(info["doc_name"]) else info["doc_name"] + ext,  # 建议文件名
            "kind": EXT_TO_KIND.get(ext, "pdf"),  # 文档大类
            "imported": (info["doc_name"], info["backend"]) in imported,  # 是否已导入语料库
        })
    # 按块数降序排列，规模大的文档通常更有代表性
    items.sort(key=lambda x: -x["block_count"])
    # 返回候选列表
    return items
