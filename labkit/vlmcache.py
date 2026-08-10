"""VLM/LLM 调用结果缓存：让反复重放同一批语料时不再重复烧钱与等待。

## 为什么需要

切分器的三个增强项最终都收敛到 `LLMBundle` 的两个方法上：

  - 图片描述     `_describe_image`  → `vision_llm_chunk` → `describe_with_prompt`
  - 艺术字兜底   `_ocr_fallback`    → `vision_llm_chunk` → `describe_with_prompt`
  - 表格摘要     `_table_summary`   → `_chat_sync`       → `async_chat`

这两个方法的输入完全确定（图片字节 / 表格 HTML + 提示词 + 模型名），输出是纯文本，
是标准的纯函数。而全语料一轮评估合计约 3400 次调用，几乎全部时间与费用都耗在这里，
可它们产出的内容对"段落怎么合并、边界切在哪里"毫无影响——正是缓存的理想对象。

## 怎么做到不改 ragflow

**不碰 ragflow 的任何源码文件**，只在实验室进程内用运行时包装替换上述两个方法。
生产的 task_executor 是另一个进程，完全不受影响。

## 缓存键为什么用内容指纹

键由 `模型名 + 方法名 + 提示词 + 载荷指纹` 四段算出 sha256，四段缺一不可：

  - 载荷指纹：核心区分位，不同图片/表格互不串味；跨文档的重复图（页眉 logo）
             指纹相同，自动共享一条缓存，命中率反而更高
  - 提示词：  同一张图会被"图片描述"和"艺术字兜底"两条链路分别请求，两者提示词
             完全不同，不进键就会互相读到对方的结果
  - 模型名：  换 VLM 后不会读到旧模型的产出
  - 版本位：  缓存格式升级时一次性作废旧数据

刻意**不用**「文档名 + 页码 + bbox」这类位置键：bbox 会随解析参数与双栏修复漂移，
键不变而图变了就会读到错图的描述且毫无察觉。内容指纹最坏只是 miss 重跑一次，
永远不会错配。

## 存储约定

  - 条目永久保留，不设 TTL；Redis 实例侧的 allkeys-lru 是唯一的兜底驱逐
  - 用独立逻辑库（见 `paths.VLM_CACHE_DB`），不与 ragflow 的消息队列混放
  - Redis 不可用、超时、数据损坏一律降级为直连模型，绝不阻塞评估
"""

import hashlib  # 导入 hashlib 计算内容指纹
import json  # 导入 json 序列化 chat 类调用的载荷与返回值
import logging  # 导入 logging 输出降级与统计信息
import threading  # 导入 threading 提供线程局部的样本标记与安装期互斥

from .paths import ensure_ragflow_importable, resolve_vlm_cache_config  # 导入路径注入与缓存配置解析

# 缓存正文键前缀。v1 是格式版本位，改变键的组成方式时递增即可整体作废旧数据
KEY_PREFIX = "chunklab:vlm:v1:"

# 样本索引键前缀。值是 set，记录该样本用到过哪些正文键，供按样本清理与分布统计
IDX_PREFIX = "chunklab:vlm:idx:"

# 连接与读写的超时秒数。缓存是加速手段而非必需品，宁可快速失败转直连，
# 也不能让一个卡住的 Redis 拖垮整轮评估
_SOCKET_TIMEOUT = 2

# 安装互斥锁，保证多线程下 monkey-patch 只发生一次
_INSTALL_LOCK = threading.Lock()

# 是否已完成安装的标记，使 install() 可被反复调用而保持幂等
_INSTALLED = False

# 被替换掉的原始方法，命中失败时回落调用它们
_ORIG_DESCRIBE = None  # 原始的 LLMBundle.describe_with_prompt
_ORIG_ASYNC_CHAT = None  # 原始的 LLMBundle.async_chat

# Redis 客户端句柄。None 表示尚未初始化，False 表示初始化失败且不再重试
_CLIENT = None

# 本进程的命中统计，评估结束后可打印出来判断缓存是否真的生效
_STATS = {"hit": 0, "miss": 0, "error": 0}

# 线程局部存储，记录当前线程正在处理哪个样本，用于把键登记进样本索引
_LOCAL = threading.local()


def set_sample(name):
    """标记当前线程正在切分哪个样本，之后写入的缓存键都会登记到该样本的索引下。

    传空值即清除标记，此后写入的键只进正文库、不进任何样本索引。
    """
    # 存入线程局部，避免 serve 的多线程场景下互相覆盖
    _LOCAL.sample = (name or "").strip()


def _current_sample():
    """取当前线程的样本标记，未设置时返回空串。"""
    # getattr 兜底，因为线程局部对象在新线程里没有该属性
    return getattr(_LOCAL, "sample", "") or ""


def _get_client():
    """懒加载 Redis 客户端，不可用时返回 None 并不再重试。"""
    # 声明使用模块级句柄
    global _CLIENT
    # 已初始化过（成功或失败）直接返回结果，避免每次调用都重连
    if _CLIENT is not None:
        # False 表示曾经失败，统一对外表现为不可用
        return _CLIENT or None
    # 读取缓存配置
    conf = resolve_vlm_cache_config()
    # 开关关闭时直接标记不可用，后续调用全部直连模型
    if not conf.get("enable"):
        # 用 False 记住"已决定不用"，与"尚未初始化"区分开
        _CLIENT = False
        return None
    # 建连失败只降级不抛出，评估照常进行
    try:
        # ragflow 用 valkey 作为 redis 协议客户端，实验室复用它以免新增依赖
        ensure_ragflow_importable()
        # 延迟导入，未启用缓存的场景不承担该模块的加载开销
        import valkey
        # 建立连接；decode_responses 保持 False，值按 bytes 存取，避免编码往返损耗
        client = valkey.Redis(
            host=conf["host"],  # 主机名，默认取自 ragflow 的 service_conf.yaml
            port=conf["port"],  # 端口，同上
            db=conf["db"],  # 实验室专用逻辑库，与生产消息队列分开
            username=conf["username"] or None,  # 空串按未设置处理
            password=conf["password"] or None,  # 空串按无密码处理
            socket_timeout=_SOCKET_TIMEOUT,  # 读写超时，防止卡死拖垮评估
            socket_connect_timeout=_SOCKET_TIMEOUT,  # 建连超时，同上
            decode_responses=False,  # 值以 bytes 收发
        )
        # 立即 ping 一次，把"连不上"暴露在这里而不是第一次读写时
        client.ping()
        # 连接可用，记入模块句柄
        _CLIENT = client
        # 打印一行连接信息，便于确认用的是哪个库
        logging.info(f"[chunklab-vlmcache] 缓存已启用：{conf['host']}:{conf['port']}/db{conf['db']}")
    except Exception as e:
        # 连不上就永久降级为直连模型，只提示一次
        logging.warning(f"[chunklab-vlmcache] Redis 不可用，本轮不使用缓存：{e}")
        # False 表示已失败且不再重试
        _CLIENT = False
    # 统一对外返回句柄或 None
    return _CLIENT or None


def _make_key(model_name, method, prompt, payload):
    """由模型名、方法名、提示词、载荷算出缓存键。

    载荷先单独摘要再进主哈希，避免把几 MB 的图片字节整体喂给主哈希对象。
    各段之间插入 \\x00 分隔，防止相邻两段的边界移动却得到同一个键。
    """
    # 载荷统一先算一次 sha256，得到定长指纹
    payload_digest = hashlib.sha256(payload).hexdigest()
    # 主哈希对象
    h = hashlib.sha256()
    # 逐段喂入，每段后跟一个 \x00 分隔符
    for part in (model_name or "", method or "", prompt or "", payload_digest):
        # 文本统一按 utf-8 编码
        h.update(part.encode("utf-8"))
        # 分隔符，保证分段无歧义
        h.update(b"\x00")
    # 拼上前缀构成完整键名
    return KEY_PREFIX + h.hexdigest()


def _cache_get(key):
    """读缓存，未命中或异常返回 None。返回值已还原为原始 Python 结构。"""
    # 取客户端，不可用直接视为未命中
    client = _get_client()
    # 无客户端时不计入命中统计，因为压根没查
    if client is None:
        return None
    # 任何读失败都降级为未命中，让调用方走真实模型
    try:
        # 按键取值
        raw = client.get(key)
        # 空值即未命中
        if raw is None:
            # 记一次未命中
            _STATS["miss"] += 1
            return None
        # 反序列化存储信封
        envelope = json.loads(raw.decode("utf-8"))
        # 记一次命中
        _STATS["hit"] += 1
        # 按存储时记录的类型还原：tuple 需要从 list 转回，其余原样返回
        if envelope.get("k") == "tuple":
            # json 只能存 list，取回时转回 tuple 以保持调用方的类型判断成立
            return tuple(envelope.get("v") or [])
        # 字符串与其它 JSON 原生类型原样返回
        return envelope.get("v")
    except Exception as e:
        # 数据损坏或连接抖动：记一次错误并按未命中处理
        _STATS["error"] += 1
        logging.debug(f"[chunklab-vlmcache] 读取失败，转直连：{e}")
        return None


def _cache_set(key, value):
    """写缓存，永久保留；失败静默忽略，不影响本次调用的返回值。"""
    # 取客户端，不可用直接跳过写入
    client = _get_client()
    # 无客户端时什么都不做
    if client is None:
        return
    # 写失败不能影响主流程，一律吞掉异常
    try:
        # 用信封记录原始类型，取回时才能还原 tuple 与 str 的区别
        envelope = {"k": "tuple" if isinstance(value, tuple) else "str", "v": list(value) if isinstance(value, tuple) else value}
        # 序列化；default=str 兜底不可序列化的元素（如某些模型返回的用量对象）
        raw = json.dumps(envelope, ensure_ascii=False, default=str).encode("utf-8")
        # 写入正文，**刻意不设 TTL**：实验室的语料是长期反复重放的，永久保留才有意义
        client.set(key, raw)
        # 取当前样本标记
        sample = _current_sample()
        # 有样本标记时把键登记进该样本的索引，供按样本清理与分布统计
        if sample:
            # 索引本身也不设 TTL，与正文保持一致
            client.sadd(IDX_PREFIX + sample, key)
    except Exception as e:
        # 写失败只影响下次命中率，不影响本次结果
        _STATS["error"] += 1
        logging.debug(f"[chunklab-vlmcache] 写入失败，忽略：{e}")


def _is_cacheable_result(value):
    """判断模型返回值是否值得永久缓存。

    两类结果必须挡住，否则一次偶发故障会被永久固化，之后每轮都命中这条坏数据：

      - 空结果：模型未产出任何内容，多半是上游异常后的降级
      - 含 `**ERROR**` 标记：ragflow 的 chat 链路遇错时**不抛异常**，而是把错误
        包装成这样的字符串返回（`_table_summary` 正是据此判定失败的）

    放过这两类只会让下一轮重新调用一次，代价远小于缓存一条永久的坏结果。
    """
    # tuple 形态（部分模型返回 (文本, 用量)）取第一项判断
    text = value[0] if isinstance(value, tuple) and value else value
    # 非字符串一律不缓存，避免存进无法可靠还原的结构
    if not isinstance(text, str):
        return False
    # 空串或纯空白视为无效结果
    if not text.strip():
        return False
    # 框架级错误标记，视为失败
    if "**ERROR**" in text:
        return False
    # 其余都是可缓存的正常结果
    return True


def _patched_describe_with_prompt(self, image, prompt):
    """`LLMBundle.describe_with_prompt` 的缓存包装：图片描述与艺术字兜底都走这里。"""
    # 载荷必须是字节才能算指纹；其它类型说明调用方式与预期不符，直接放行不缓存
    if not isinstance(image, (bytes, bytearray)):
        # 原样调用真实模型，保证行为不因缓存层而改变
        return _ORIG_DESCRIBE(self, image, prompt)
    # 模型名进键，换模型后不会读到旧模型的产出
    key = _make_key(getattr(self, "llm_name", ""), "describe_with_prompt", prompt or "", bytes(image))
    # 先查缓存
    cached = _cache_get(key)
    # 命中且是字符串就直接返回，省掉一次网络调用与 token 消耗
    if isinstance(cached, str):
        return cached
    # 未命中：调用真实模型
    result = _ORIG_DESCRIBE(self, image, prompt)
    # 仅缓存有效结果；空结果与错误标记放过，避免把一次偶发故障永久固化
    if _is_cacheable_result(result):
        # 写入缓存供后续轮次复用
        _cache_set(key, result)
    # 返回真实结果
    return result


async def _patched_async_chat(self, system, history, gen_conf={}, **kwargs):
    """`LLMBundle.async_chat` 的缓存包装：表格摘要走这里。

    必须保持 async 定义——调用方 `_chat_sync` 会对返回值 `asyncio.run`，
    换成同步函数会立刻报错。
    """
    # 载荷 = 系统提示 + 对话历史 + 生成参数 + 其余关键字参数，任何一项变化都应换一条缓存
    try:
        # sort_keys 保证字典顺序不同不会算出不同的键；default=str 兜底非常规类型
        payload = json.dumps([system, history, gen_conf, kwargs], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    except Exception:
        # 载荷无法稳定序列化时不缓存，直接调真实模型，避免算出不可靠的键
        return await _ORIG_ASYNC_CHAT(self, system, history, gen_conf, **kwargs)
    # 系统提示已并入载荷，此处提示词位留空，避免同一内容被算进键两次
    key = _make_key(getattr(self, "llm_name", ""), "async_chat", "", payload)
    # 先查缓存
    cached = _cache_get(key)
    # 命中即返回；str 与 tuple 都是调用方能正确处理的形态
    if isinstance(cached, (str, tuple)):
        return cached
    # 未命中：await 真实模型
    result = await _ORIG_ASYNC_CHAT(self, system, history, gen_conf, **kwargs)
    # 仅缓存有效结果；chat 链路遇错会返回 **ERROR** 字符串而非抛异常，必须在这里挡住
    if _is_cacheable_result(result):
        # 写入缓存供后续轮次复用
        _cache_set(key, result)
    # 返回真实结果
    return result


def install():
    """安装缓存包装。幂等，可在每次切分前无脑调用。

    返回 True 表示缓存生效，False 表示未启用或 ragflow 模块导入失败。
    """
    # 声明使用模块级标记与原始方法句柄
    global _INSTALLED, _ORIG_DESCRIBE, _ORIG_ASYNC_CHAT
    # 缓存开关关闭时不做任何替换，保持 ragflow 原始行为
    if not resolve_vlm_cache_config().get("enable"):
        return False
    # 加锁保证多线程下只安装一次
    with _INSTALL_LOCK:
        # 已安装直接返回，避免把包装函数再包一层导致无限递归
        if _INSTALLED:
            return True
        # 导入失败只降级为不缓存，不影响纯切分模式
        try:
            # 注入 ragflow 源码路径后才能导入其模块
            ensure_ragflow_importable()
            # 延迟导入被包装的类
            from api.db.services.llm_service import LLMBundle
            # 记录原始方法，命中失败时回落调用
            _ORIG_DESCRIBE = LLMBundle.describe_with_prompt
            # 异步方法同样先记录再替换
            _ORIG_ASYNC_CHAT = LLMBundle.async_chat
            # 换上带缓存的版本；只改本进程内的类属性，ragflow 源码文件一行未动
            LLMBundle.describe_with_prompt = _patched_describe_with_prompt
            # 异步方法同上
            LLMBundle.async_chat = _patched_async_chat
            # 标记已安装
            _INSTALLED = True
            return True
        except Exception as e:
            # 装不上就按无缓存运行，评估结果依然正确，只是慢
            logging.warning(f"[chunklab-vlmcache] 安装失败，本轮不使用缓存：{e}")
            return False


def stats():
    """返回本进程的命中统计，供评估结束后打印。"""
    # 返回副本，避免调用方误改内部状态
    return dict(_STATS)


def reset_stats():
    """清零本进程的命中统计，用于分轮次统计。"""
    # 逐项归零，保持同一个字典对象
    for k in _STATS:
        _STATS[k] = 0


def _scan_keys(client, pattern):
    """按模式增量扫描键名，避免 KEYS 在大库上阻塞 Redis。"""
    # scan_iter 内部按游标分批，单次不会长时间占用服务端
    return list(client.scan_iter(match=pattern, count=500))


def cache_stats():
    """统计缓存库中的条目数与占用字节，返回可直接打印的字典。"""
    # 取客户端，不可用时返回明确的错误说明而非抛异常
    client = _get_client()
    # 无客户端说明缓存未启用或连不上
    if client is None:
        return {"available": False, "reason": "缓存未启用或 Redis 不可用"}
    # 扫描全部正文键
    keys = _scan_keys(client, KEY_PREFIX + "*")
    # 累计值的字节数，用 STRLEN 而非 MEMORY USAGE，后者每键一次往返且开销更大
    total_bytes = 0
    # 分批用管道取长度，减少网络往返
    for start in range(0, len(keys), 500):
        # 每批最多 500 个键
        batch = keys[start:start + 500]
        # 构造管道
        pipe = client.pipeline()
        # 逐键排入 STRLEN
        for k in batch:
            pipe.strlen(k)
        # 一次性执行并累加
        total_bytes += sum(x for x in pipe.execute() if isinstance(x, int))
    # 扫描样本索引键，统计每个样本用到多少条缓存
    per_sample = {}
    # 逐个索引键取集合大小
    for idx_key in _scan_keys(client, IDX_PREFIX + "*"):
        # 键名去掉前缀就是样本名
        name = idx_key.decode("utf-8", "ignore")[len(IDX_PREFIX):]
        # scard 只取基数，不拉取集合内容
        per_sample[name] = client.scard(idx_key)
    # 组装统计结果
    return {
        "available": True,  # 缓存可用
        "entries": len(keys),  # 正文条目总数
        "bytes": total_bytes,  # 正文占用字节总数
        "per_sample": dict(sorted(per_sample.items(), key=lambda kv: -kv[1])),  # 按用量倒序的样本分布
    }


def cache_clear(sample=None):
    """清理缓存。不传样本名即全清，传样本名只清该样本索引下的条目。

    注意：跨文档重复的图片（页眉 logo 等）会被多个样本共享同一条缓存，
    按样本清理时会把这类共享条目一并删掉，其它样本下次重放会 miss 重跑一次。
    只多花一次调用，不会产生错误结果，因此不做引用计数。
    """
    # 取客户端，不可用时返回明确说明
    client = _get_client()
    # 无客户端说明缓存未启用或连不上
    if client is None:
        return {"available": False, "reason": "缓存未启用或 Redis 不可用"}
    # 指定样本：只删该样本索引里登记的键
    if sample:
        # 索引键名
        idx_key = IDX_PREFIX + sample
        # 取出该样本用到的全部正文键
        members = list(client.smembers(idx_key))
        # 有条目才执行删除
        if members:
            # 一次性删除全部正文键
            client.delete(*members)
        # 最后删掉索引本身
        client.delete(idx_key)
        # 返回删除数量
        return {"available": True, "deleted": len(members), "scope": sample}
    # 未指定样本：全清正文键与全部样本索引
    keys = _scan_keys(client, KEY_PREFIX + "*") + _scan_keys(client, IDX_PREFIX + "*")
    # 分批删除，避免单条命令参数过多
    for start in range(0, len(keys), 500):
        # 每批最多 500 个键
        batch = keys[start:start + 500]
        # 批内非空才发命令
        if batch:
            client.delete(*batch)
    # 返回删除数量
    return {"available": True, "deleted": len(keys), "scope": "全部"}
