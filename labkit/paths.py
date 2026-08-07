"""路径常量与 ragflow 源码注入。

本模块是 chunk-lab 唯一知道 ragflow 仓库位置的地方，其余模块一律从这里取路径，
避免把 ragflow 的绝对路径散落到各处，将来搬迁只需要改这一个文件。
"""

import sys  # 导入 sys 以便把 ragflow 源码根目录插入模块搜索路径
from pathlib import Path  # 导入 Path 做跨平台路径拼接，避免手写字符串分隔符

# chunk-lab 仓库根目录：本文件位于 <root>/labkit/paths.py，故上溯两级得到仓库根
LAB_ROOT = Path(__file__).resolve().parent.parent

# ragflow 后端仓库根目录：与 chunk-lab 平级，是被测代码所在地（本实验室只读它，不写它）
RAGFLOW_ROOT = LAB_ROOT.parent / "ragflow"

# 语料目录：每个样本一个子目录，存放原始文件与 MinerU 缓存产物
CORPUS_DIR = LAB_ROOT / "corpus"

# 回归基线目录：存放历次快照，纳入 chunk-lab 自己的版本管理以便追溯指标变化
BASELINE_DIR = LAB_ROOT / "baselines"

# 报告目录：每轮运行的输出，属于易变产物，由 .gitignore 排除
REPORT_DIR = LAB_ROOT / "reports"


def ensure_ragflow_importable():
    """把 ragflow 源码根目录加入 sys.path，使 `rag.app.*` 等模块可被直接导入。

    ragflow 内部使用 `from rag.app...`、`from common...` 这类以仓库根为基准的绝对导入，
    因此必须把仓库根本身（而不是它的父目录）放进 sys.path。
    """
    # 先确认 ragflow 仓库真实存在，避免路径写错时抛出难以理解的 ImportError
    if not RAGFLOW_ROOT.is_dir():
        # 直接报出期望路径，便于使用者一眼看出是目录结构不符而非代码缺陷
        raise RuntimeError(f"未找到 ragflow 仓库：{RAGFLOW_ROOT}")
    # 转成字符串，因为 sys.path 的元素必须是 str 而不是 Path
    root = str(RAGFLOW_ROOT)
    # 仅在尚未注入时插入，重复调用保持幂等，避免 sys.path 被同一路径塞满
    if root not in sys.path:
        # 插到最前面，确保同名模块优先命中 ragflow 的实现而不是第三方包
        sys.path.insert(0, root)
    # 返回注入后的路径，方便调用方打印诊断信息确认用的是哪个克隆
    return root
