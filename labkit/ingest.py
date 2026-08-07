"""语料导入：把本机 MinerU 产物收编成稳定的实验语料库。

产物原本散落在 ~/MinerU/result 与 RagFlow/tmp 下，目录名带随机后缀且可能被清理。
本模块把选中的 content_list.json 复制进 corpus/，并生成描述文件 case.yaml，
使语料不受上游目录变动影响，且每个样本的切分参数可复现。
"""

import json  # 导入 json 用于读取块数以便写入描述文件
import shutil  # 导入 shutil 执行文件复制
from pathlib import Path  # 导入 Path 统一处理路径

import yaml  # 导入 yaml 生成人可读的样本描述文件

from .paths import CORPUS_DIR  # 导入语料根目录常量

# 本机 MinerU 产物根目录，语料清单中的相对路径以它为基准
MINERU_RESULT = Path("/Users/jialei/MinerU/result")
# RagFlow 侧的实验产物根目录，双栏诊断等构造样例存放于此
RAGFLOW_TMP = Path("/Users/jialei/Desktop/RagFlow/tmp/pdfs")

# 语料清单：每项描述一个实验样本
# 字段含义：
#   case_id   语料目录名，同时是报告中的样本标识
#   source    content_list.json 的绝对路径
#   filename  原始文件名（含扩展名），切分器据此判断文档类型
#   kind      文档大类，用于报告分组
#   slide     是否启用 PPTX 的 slide_mode
#   note      该样本的关注点，说明为什么把它收进语料库
CASES = [
    {
        "case_id": "twocol_diag",
        "source": RAGFLOW_TMP / "mineru_two_column_e2e_output/mineru_two_column_diagnostic_auto_sm1so2ql/hybrid_auto/mineru_two_column_diagnostic_content_list.json",
        "filename": "mineru_two_column_diagnostic.pdf",
        "kind": "pdf_twocolumn",
        "slide": False,
        "note": "双栏 PDF 基准样例，阶段一保真度验证用的就是它，切分边界已与生产逐块比对过",
    },
    {
        "case_id": "law_criminal",
        "source": MINERU_RESULT / "中华人民共和国刑法（2020修正）(1)_auto_wttcqvk5/hybrid_auto/中华人民共和国刑法（2020修正）(1)_content_list.json",
        "filename": "中华人民共和国刑法（2020修正）.pdf",
        "kind": "pdf_law",
        "slide": False,
        "note": "长法律文本，条文层级密集，检验标题边界与条文完整性",
    },
    {
        "case_id": "annual_csrc",
        "source": MINERU_RESULT / "中国证券监督管理委员会年报（2024年）_auto_2m8ctt6u/vlm/中国证券监督管理委员会年报（2024年）_content_list.json",
        "filename": "中国证券监督管理委员会年报（2024年）.pdf",
        "kind": "pdf_annual",
        "slide": False,
        "note": "年报，表格与图表密集，vlm backend 产物",
    },
    {
        "case_id": "annual_cnipa",
        "source": MINERU_RESULT / "国家知识产权局2025年度报告_auto_8krvefl7/hybrid_auto/国家知识产权局2025年度报告_content_list.json",
        "filename": "国家知识产权局2025年度报告.pdf",
        "kind": "pdf_annual",
        "slide": False,
        "note": "年报，hybrid backend 产物，与 annual_csrc 形成 backend 对照",
    },
    {
        "case_id": "patent_cn",
        "source": MINERU_RESULT / "2006-cn-092586_auto_v0an0fqn/hybrid_auto/2006-cn-092586_content_list.json",
        "filename": "2006-cn-092586.pdf",
        "kind": "pdf_patent",
        "slide": False,
        "note": "专利文献，权利要求编号与附图说明结构特殊",
    },
    {
        "case_id": "pptx_training",
        "source": MINERU_RESULT / "知识平台使用培训20260723_auto_i26l92hi/office/知识平台使用培训20260723_content_list.json",
        "filename": "知识平台使用培训20260723.pptx",
        "kind": "pptx",
        "slide": True,
        "note": "培训 PPT，slide_mode 的主要验证样本，含多个表格",
    },
    {
        "case_id": "pptx_pytorch",
        "source": MINERU_RESULT / "01-PyTorch基本使用_auto_d4bojr2j/office/01-PyTorch基本使用_content_list.json",
        "filename": "01-PyTorch基本使用.pptx",
        "kind": "pptx",
        "slide": True,
        "note": "教学 PPT，含代码块与公式，检验碎字与代码折行",
    },
    {
        "case_id": "docx_cert_manual",
        "source": MINERU_RESULT / "附件4：支付系统测试数字证书制作指导手册.docx_auto_s3l_3u12/office/附件4：支付系统测试数字证书制作指导手册.docx_content_list.json",
        "filename": "附件4：支付系统测试数字证书制作指导手册.docx",
        "kind": "docx",
        "slide": False,
        "note": "操作手册 DOCX，检验 DOCX 目录治理与步骤列表完整性",
    },
    {
        "case_id": "docx_union_bad",
        "source": MINERU_RESULT / "正文-关于修订《龙盈智达（北京）科技有限公司工会委员会财务管理办法》的通知-有问题的_auto_8wjkf617/office/正文-关于修订《龙盈智达（北京）科技有限公司工会委员会财务管理办法》的通知-有问题的_content_list.json",
        "filename": "正文-关于修订工会委员会财务管理办法的通知.docx",
        "kind": "docx",
        "slide": False,
        "note": "产物目录名自带「有问题的」标记，是已知的问题样本，应当能被检测器命中",
    },
    {
        "case_id": "law_bill_hybrid",
        "source": MINERU_RESULT / "最高人民法院关于审理票据纠纷案件若干问题的规定（2020修正）_auto_tlabfcrt/hybrid_auto/最高人民法院关于审理票据纠纷案件若干问题的规定（2020修正）_content_list.json",
        "filename": "最高人民法院关于审理票据纠纷案件若干问题的规定（2020修正）.pdf",
        "kind": "pdf_law",
        "slide": False,
        "note": "backend 对照 A：hybrid 产物 137 块，与 law_bill_vlm 同源不同 backend",
    },
    {
        "case_id": "law_bill_vlm",
        "source": MINERU_RESULT / "最高人民法院关于审理票据纠纷案件若干问题的规定（2020修正）(1)_auto_weueglgr/vlm/最高人民法院关于审理票据纠纷案件若干问题的规定（2020修正）(1)_content_list.json",
        "filename": "最高人民法院关于审理票据纠纷案件若干问题的规定（2020修正）.pdf",
        "kind": "pdf_law",
        "slide": False,
        "note": "backend 对照 B：vlm 产物 111 块，同一文档比 hybrid 少 26 块，用于检验切分逻辑对 backend 差异的鲁棒性",
    },
    {
        "case_id": "xlsx_9sheet",
        "source": MINERU_RESULT / "复杂表格样例_9sheet_auto_xhfvnie1/office/复杂表格样例_9sheet_content_list.json",
        "filename": "复杂表格样例_9sheet.xlsx",
        "kind": "xlsx",
        "slide": False,
        "note": "多 sheet 复杂表格，检验表格独立成块与表头重复",
    },
    {
        "case_id": "xlsx_ghost_row",
        "source": MINERU_RESULT / "复杂表格样例_9sheet_幽灵行2(1)_auto_zmz78qmo/office/复杂表格样例_9sheet_幽灵行2(1)_content_list.json",
        "filename": "复杂表格样例_9sheet_幽灵行2.xlsx",
        "kind": "xlsx",
        "slide": False,
        "note": "已知的幽灵行问题样本，与 xlsx_9sheet 形成对照",
    },
]


def ingest_case(case, overwrite=False):
    """把单个样本复制进语料库并生成 case.yaml，返回该样本目录。"""
    # 源产物路径
    source = Path(case["source"])
    # 目标语料目录，以 case_id 命名
    dest_dir = CORPUS_DIR / case["case_id"]
    # 目标产物路径，统一重命名为 content_list.json，屏蔽上游命名差异
    dest_json = dest_dir / "content_list.json"
    # 源文件不存在时跳过并回报，避免整批导入因个别样本失败而中断
    if not source.is_file():
        return None, f"源产物不存在：{source}"
    # 已导入且未要求覆盖时直接跳过，使重复执行保持幂等
    if dest_json.is_file() and not overwrite:
        return dest_dir, "已存在，跳过"
    # 创建样本目录，parents 允许语料根目录尚未建立，exist_ok 允许重复导入
    dest_dir.mkdir(parents=True, exist_ok=True)
    # 复制产物，copy2 保留修改时间便于追溯产物新鲜度
    shutil.copy2(source, dest_json)
    # 读取块数写进描述文件，便于在不打开产物的情况下了解样本规模
    with dest_json.open("r", encoding="utf-8") as fh:
        # 解析后取长度即为 MinerU 原始块数量
        block_count = len(json.load(fh))
    # 组装描述文件内容，source 记录原始路径以便日后追溯产物来源
    meta = {
        "case_id": case["case_id"],  # 样本标识
        "filename": case["filename"],  # 原始文件名，决定切分器的类型判断
        "kind": case["kind"],  # 文档大类，报告分组用
        "slide_mode": case["slide"],  # 是否按幻灯片切分
        "block_count": block_count,  # MinerU 原始块数
        "source": str(source),  # 产物原始路径，仅作追溯用途
        "note": case["note"],  # 该样本的关注点
    }
    # 写出 YAML 描述文件，allow_unicode 保证中文不被转义成 \uXXXX
    with (dest_dir / "case.yaml").open("w", encoding="utf-8") as fh:
        # sort_keys=False 保持字段书写顺序，便于人工阅读
        yaml.safe_dump(meta, fh, allow_unicode=True, sort_keys=False)
    # 返回目录与状态说明
    return dest_dir, f"已导入 {block_count} 块"


def ingest_all(overwrite=False):
    """批量导入清单中的全部样本，返回逐条结果。"""
    # 收集每个样本的导入结果，供调用方打印汇总
    results = []
    # 逐个导入，单个失败不影响其余样本
    for case in CASES:
        # 执行导入并捕获状态
        dest, status = ingest_case(case, overwrite=overwrite)
        # 记录样本标识与状态
        results.append((case["case_id"], status))
    # 返回全部结果
    return results
