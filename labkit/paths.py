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


def resolve_scan_dirs():
    """解析「添加语料」页要扫描哪些 MinerU 产物目录。

    默认把实验室自己的产物目录排在最前，其后才是外部目录（生产的 MinerU
    输出、历史实验产物）。外部目录保留是为了能导入既有产物，但界面会标明
    来源，避免分不清哪些是实验室产的、哪些是生产产的。

    可通过 labconfig.json 的 scan_dirs 完全覆盖，例如只留实验室目录。
    """
    # 配置了就完全以配置为准，允许使用者只扫实验室目录
    configured = load_config().get("scan_dirs")
    # 接受字符串列表，逐个展开 ~
    if isinstance(configured, list) and configured:
        return [Path(p).expanduser() for p in configured if p]
    # 未配置时用默认清单：实验室产物优先，外部目录其次
    return [
        MINERU_OUT,  # 实验室自己的解析产物
        Path.home() / "MinerU" / "result",  # 生产 MinerU 服务的输出目录
        LAB_ROOT.parent / "ragflow" / "tmp" / "pdfs",  # 历史实验产物
    ]


# 待扫描的产物目录清单
SCAN_DIRS = resolve_scan_dirs()


def resolve_mineru_token():
    """读取 MinerU 官方云端 API 的 token。

    优先级：环境变量 > 本机配置文件。刻意不接受从请求体传入——
    token 属于长期凭据，随请求传递会散落到日志与浏览器历史里。
    """
    # 环境变量优先，便于临时切换账号
    env = os.environ.get("MINERU_API_TOKEN")
    # 非空即采用
    if env:
        return env.strip()
    # 其次读本机配置文件
    return str(load_config().get("mineru_token") or "").strip()


def resolve_cloud_backend():
    """云端解析使用的 backend，默认 vlm-engine。"""
    # 环境变量优先
    env = os.environ.get("MINERU_CLOUD_BACKEND")
    # 非空即采用
    if env:
        return env.strip()
    # 其次读配置，最后回落默认
    return str(load_config().get("mineru_cloud_backend") or "vlm-engine").strip()


# 本地解析的默认 backend。
# 桥接层 `chunklab_bridge.parse.resolve_mineru_config` 的内置默认是 `pipeline`，
# 那是对齐 ragflow 生产的取值；实验室这边跑的是本机 VLM 部署，默认用 `vlm-engine`
# 才与实际使用一致，故在 chunk-lab 侧覆盖，不改桥接层。
DEFAULT_LOCAL_BACKEND = "vlm-engine"


def resolve_local_backend():
    """本地解析使用的 backend，默认 vlm-engine。

    优先级与云端那条对称：环境变量 > 本机配置文件 > 默认值。
    环境变量沿用桥接层认的 `MINERU_BACKEND`，避免同一件事出现两个变量名。
    """
    # 环境变量优先，且与桥接层读的是同一个变量，语义保持一致
    env = os.environ.get("MINERU_BACKEND")
    # 非空即采用
    if env:
        return env.strip()
    # 其次读配置，最后回落默认
    return str(load_config().get("mineru_local_backend") or DEFAULT_LOCAL_BACKEND).strip()


def resolve_tenant_id():
    """读取用于加载 LLM/VLM 的租户标识。

    切分器的三个增强项（表格摘要、图片描述、艺术字兜底）都要按租户去库里查模型配置，
    没有租户就一律降级为空结果。离线实验室本身不属于任何租户，因此由本机配置指定
    一个真实租户来借用其模型授权。

    优先级：环境变量 > 本机配置文件。留空即保持无模型的纯切分行为。
    """
    # 环境变量优先，便于临时切换账号而不改文件
    env = os.environ.get("CHUNKLAB_TENANT_ID")
    # 非空即采用
    if env:
        return env.strip()
    # 其次读本机配置文件（不入版本管理，租户标识不会被提交）
    return str(load_config().get("tenant_id") or "").strip()


# ragflow 服务配置中 redis 段的默认值，仅在读取 service_conf.yaml 失败时兜底
_REDIS_FALLBACK = {"host": "127.0.0.1", "port": 6379, "username": "", "password": ""}

# 实验室缓存专用的 Redis 逻辑库编号。
# 刻意避开 ragflow 占用的 db=1：实验室缓存与生产的消息队列共处一库时，
# 一次误清库就会波及生产任务队列，分库是最省心的隔离方式。
VLM_CACHE_DB = 3


def _read_ragflow_redis_conf():
    """读取 ragflow 的 service_conf.yaml 中的 redis 连接参数。

    刻意只读 YAML 文件而不调 ragflow 的 settings 初始化：后者会顺带建立
    Elasticsearch 等连接，离线实验室不该依赖那些服务起没起。
    """
    # 复制一份兜底值，解析失败时原样返回，不让调用方拿到 None
    conf = dict(_REDIS_FALLBACK)
    # 配置缺失或格式异常都只降级为兜底值，不影响切分主流程
    try:
        # 延迟导入 yaml，未使用缓存的场景不承担该模块的加载开销
        import yaml
        # ragflow 服务配置文件，与生产读取的是同一份
        with (RAGFLOW_ROOT / "conf" / "service_conf.yaml").open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        # 取出 redis 段，非字典则按空配置处理
        section = data.get("redis") or {}
        # host 字段在 ragflow 中是 "127.0.0.1:6379" 这种带端口的写法，需要拆开
        raw_host = str(section.get("host") or "").strip()
        # 含冒号时按 host:port 拆分
        if ":" in raw_host:
            # 从右侧拆一次，兼容 IPv6 之外的常规写法
            host_part, _, port_part = raw_host.rpartition(":")
            # 主机名非空才覆盖兜底值
            if host_part:
                conf["host"] = host_part
            # 端口必须是数字才覆盖，非法值保持兜底
            if port_part.isdigit():
                conf["port"] = int(port_part)
        # 不含冒号时整串就是主机名，端口沿用默认
        elif raw_host:
            conf["host"] = raw_host
        # 用户名与密码直接透传，ragflow 默认用户名为空
        conf["username"] = str(section.get("username") or "")
        # 密码同上，空串表示无需认证
        conf["password"] = str(section.get("password") or "")
    except Exception:
        # 读不到就用兜底值，连接失败会在缓存层降级为直连模型
        pass
    # 返回解析结果
    return conf


def resolve_vlm_cache_config():
    """解析 VLM/LLM 结果缓存的配置。

    连接参数默认复用 ragflow 的 redis 配置（同一个实例，避免重复维护密码），
    但**逻辑库固定另开一个**，不与生产的消息队列混放。

    优先级：环境变量 CHUNKLAB_VLM_CACHE（仅控制开关）> labconfig.json 的
    vlm_cache 块 > ragflow service_conf.yaml > 内置兜底值。
    """
    # 先取 ragflow 的连接参数作为基线
    conf = _read_ragflow_redis_conf()
    # 缓存默认开启：实验室的核心诉求就是反复重放同一批语料
    conf["enable"] = True
    # 逻辑库默认使用实验室专用编号
    conf["db"] = VLM_CACHE_DB
    # 读本机配置中的 vlm_cache 块，允许逐项覆盖连接参数
    section = load_config().get("vlm_cache")
    # 仅当配置块是字典时才逐项覆盖，写错类型直接忽略而不报错
    if isinstance(section, dict):
        # 遍历允许覆盖的字段，未出现的字段保持基线值
        for field in ("enable", "host", "port", "db", "username", "password"):
            # 仅在显式给值时覆盖，None 不参与
            if section.get(field) is not None:
                conf[field] = section[field]
    # 环境变量最后生效，且只管开关，便于临时跑一轮不吃缓存的对照实验
    env = os.environ.get("CHUNKLAB_VLM_CACHE")
    # 显式给值时按常见的假值字面量判定
    if env is not None:
        # "0"/"false"/"no"/"off"/空串一律视为关闭，其余视为开启
        conf["enable"] = env.strip().lower() not in ("0", "false", "no", "off", "")
    # 端口与库号统一转成整数，容忍配置里写成字符串
    conf["port"] = int(conf["port"])
    # 库号同上
    conf["db"] = int(conf["db"])
    # 开关统一转成布尔
    conf["enable"] = bool(conf["enable"])
    # 返回最终配置
    return conf


# ragflow 模型厂商清单是否已注入，避免重复读文件
_LLM_READY = False


def ensure_ragflow_llm_ready():
    """补齐 ragflow 加载模型所必需的全局配置，成功返回 True。

    `TenantLLMService.split_model_name_and_factory` 依赖 `settings.FACTORY_LLM_INFOS`
    才能把 "qwen3-vl-plus@Tongyi-Qianwen" 拆成模型名与厂商；该全局量在 ragflow 服务进程里
    由 `init_settings()` 填充，而实验室是独立进程，不补就会报 `Model(...@None) not authorized`。

    **刻意不调 `init_settings()`**：它还会建立 Elasticsearch 连接，而离线实验室不该依赖
    检索引擎起没起。这里只读 `conf/llm_factories.json` 补上厂商清单，实测足够。
    """
    # 声明使用模块级标记，保证只注入一次
    global _LLM_READY
    # 已注入过直接返回
    if _LLM_READY:
        return True
    # 注入前必须保证 ragflow 可导入
    ensure_ragflow_importable()
    # 缺文件或格式异常都只降级为「无模型」，不影响纯切分
    try:
        # 延迟导入，未启用模型的场景不承担该模块的加载开销
        from common import settings
        # 已经有清单说明别处初始化过，不重复覆盖
        if not settings.FACTORY_LLM_INFOS:
            # 厂商清单与 ragflow 服务读取的是同一份文件
            with (RAGFLOW_ROOT / "conf" / "llm_factories.json").open("r", encoding="utf-8") as fh:
                settings.FACTORY_LLM_INFOS = json.load(fh)["factory_llm_infos"]
        # 标记已就绪
        _LLM_READY = True
        return True
    except Exception:
        # 补不上就让调用方按无模型处理，不抛出中断切分
        return False


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
