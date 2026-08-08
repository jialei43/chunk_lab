"""路径常量与 ragflow 源码注入。

本模块是 chunk-lab 唯一知道 ragflow 仓库位置的地方，其余模块一律从这里取路径，
避免把 ragflow 的绝对路径散落到各处，将来搬迁只需要改这一个文件。
"""

import json  # 导入 json 读取本机配置文件
import os  # 导入 os 读取环境变量
import sys  # 导入 sys 以便把 ragflow 源码根目录插入模块搜索路径
from pathlib import Path  # 导入 Path 做跨平台路径拼接，避免手写字符串分隔符

# chunk-lab 仓库根目录：本文件位于 <root>/labkit/paths.py，故上溯两级得到仓库根
LAB_ROOT = Path(__file__).resolve().parent.parent

# ragflow 后端仓库根目录：与 chunk-lab 平级，是被测代码所在地（本实验室只读它，不写它）
RAGFLOW_ROOT = LAB_ROOT.parent / "ragflow"

# 本机配置文件：记录数据目录等因机器而异的设置，不纳入版本管理
CONFIG_FILE = LAB_ROOT / "labconfig.json"

# 数据根目录的默认位置。
# 刻意放在仓库之外：语料含大量 middle.json（实测 20MB+），加上解析产物与
# 历史快照，留在仓库内会让 IDE 索引整个项目时明显变慢。仓库内只保留代码。
DEFAULT_DATA_ROOT = Path.home() / "MinerU" / "chunk_lab"


def load_config():
    """读取本机配置文件，缺失或损坏时返回空配置。"""
    # 配置文件不存在属正常情况，使用默认值即可
    if not CONFIG_FILE.is_file():
        return {}
    # 配置损坏不应让整个工具不可用，降级为默认配置
    try:
        # 读取并解析
        with CONFIG_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        # 仅接受字典结构
        return data if isinstance(data, dict) else {}
    except Exception:
        # 解析失败按空配置处理
        return {}


def resolve_data_root():
    """解析数据根目录。

    优先级：环境变量 > 本机配置文件 > 默认位置。
    与 ragflow 解析 MinerU 配置的优先级一致，便于理解与临时覆盖。
    """
    # 环境变量优先，便于临时切换而不改文件
    env = os.environ.get("CHUNKLAB_DATA_DIR")
    # 非空时展开 ~ 后采用
    if env:
        return Path(env).expanduser().resolve()
    # 其次读本机配置文件
    configured = load_config().get("data_dir")
    # 配置了才采用
    if configured:
        return Path(configured).expanduser().resolve()
    # 最后回落到默认位置
    return DEFAULT_DATA_ROOT


# 数据根目录：语料、历史轮次、报告、解析产物全部放在这里，与代码分离
DATA_ROOT = resolve_data_root()

# 语料目录：每个样本一个子目录，存放 MinerU 产物与样本描述
CORPUS_DIR = DATA_ROOT / "corpus"

# 回归基线目录：存放指向某一轮的基准指针
BASELINE_DIR = DATA_ROOT / "baselines"

# 报告目录：每轮运行的 Markdown 评估报告
REPORT_DIR = DATA_ROOT / "reports"

# 历史轮次目录：每轮的完整快照与切分文本
RUNS_DIR = DATA_ROOT / "runs"

# MinerU 解析产物目录，与生产的 MinerU 输出目录分开
MINERU_OUT = DATA_ROOT / "mineru_out"

# 上传文件的暂存目录
UPLOAD_DIR = DATA_ROOT / "uploads"


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
