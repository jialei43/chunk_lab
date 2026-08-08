"""MinerU 官方云端 API 客户端（精准解析 API v4）。

本地解析一份 PDF 要十几分钟，云端通常快得多，且支持一次提交至多 200 个文件。

与本地解析的关键差异：
  - 异步：提交后要轮询，不是一次请求拿结果；
  - 批量：一次申请多个上传链接，共用一个 batch_id 轮询；
  - 产物：返回 Zip，需解压后转成与本地解析同构的目录结构，
    否则 chunk-lab 后续的切分、评估、语料导入全都要改。

官方限制（精准解析 API）：单文件 ≤ 200MB、≤ 200 页、单批 ≤ 200 个文件。
"""

import io  # 导入 io 在内存中处理下载的 zip
import logging  # 导入 logging 输出解析过程
import time  # 导入 time 控制轮询间隔
import zipfile  # 导入 zipfile 解压结果包
from pathlib import Path  # 导入 Path 处理产物路径

import requests  # 导入 requests 调用官方 API

from .paths import resolve_cloud_backend, resolve_mineru_token  # 导入凭据与 backend 配置

# 官方 API 基址
API_BASE = "https://mineru.net/api/v4"
# 申请批量上传链接
URL_BATCH = f"{API_BASE}/file-urls/batch"
# 批量任务结果查询，末尾拼 batch_id
URL_BATCH_RESULT = f"{API_BASE}/extract-results/batch"

# 官方限制：单批最多 200 个文件，超出需分批提交
MAX_BATCH = 200
# 单文件大小上限 200MB，超出直接拒绝而不是提交后才失败
MAX_FILE_BYTES = 200 * 1024 * 1024
# 轮询间隔（秒）。太密会浪费配额且无意义，解析本身以分钟计
POLL_INTERVAL = 5
# 轮询总时长上限（秒），防止任务异常时无限等待
POLL_TIMEOUT = 3600

log = logging.getLogger("mineru-cloud")


class MinerUCloudError(RuntimeError):
    """云端解析相关的错误，便于调用方与本地解析的异常区分开。"""


def _headers(token):
    """构造鉴权请求头。"""
    # 官方要求 Bearer 鉴权
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# 官方错误码到可操作提示的映射。
# 原始报文形如 {"msgCode":"A0211","msg":"user token expired"}，
# 直接抛给使用者看不出该做什么，故翻译成明确的下一步。
ERROR_HINTS = {
    "A0211": "token 已过期，请到 MinerU 控制台重新生成，并更新 labconfig.json 的 mineru_token",
    "A0212": "token 无效，请检查是否复制完整（不要带引号或空格）",
    "A0202": "token 缺失或格式不对，应为 Bearer 形式的密钥",
}


def _friendly(body):
    """把官方错误报文翻译成可操作的提示。"""
    # 取出错误码与原始消息
    code = str(body.get("msgCode") or "")
    raw = body.get("msg") or ""
    # 已知错误码给出明确指引，并保留原始消息便于对照
    hint = ERROR_HINTS.get(code)
    if hint:
        return f"{hint}（原始报错：{raw}）"
    # 未知错误码原样返回
    return raw or str(body)[:200]


def _check(resp, what):
    """校验响应并取出 data 段。

    官方接口即便 HTTP 200，业务失败也会在 code 字段里体现，
    只看状态码会把失败当成功。
    """
    # HTTP 层失败：先尝试解析报文给出可操作提示，而不是甩一串原始 JSON
    if resp.status_code != 200:
        # 报文可能不是 JSON，解析失败时退回原文
        try:
            body = resp.json()
        except Exception:
            body = {}
        # 鉴权类错误单独标注，这是最常见的失败原因
        if resp.status_code == 401:
            raise MinerUCloudError(f"{what} 失败（鉴权未通过）：{_friendly(body)}")
        raise MinerUCloudError(f"{what} 失败：HTTP {resp.status_code} "
                               f"{_friendly(body) if body else resp.text[:200]}")
    # 解析响应体
    try:
        body = resp.json()
    except Exception:
        raise MinerUCloudError(f"{what} 返回的不是 JSON：{resp.text[:200]}")
    # 业务码非 0 视为失败
    if body.get("code") not in (0, 200):
        raise MinerUCloudError(f"{what} 失败：{_friendly(body)}")
    # 返回数据段
    return body.get("data") or {}


def submit_batch(paths, token=None, backend=None, lang="ch", is_ocr=False,
                 enable_formula=True, enable_table=True):
    """申请批量上传链接并上传文件，返回 batch_id 与文件名映射。

    官方流程是「先申请链接、再 PUT 上传」，而不是直接 multipart 提交。
    """
    # 读取凭据；缺失时给出明确的配置指引而不是让请求 401
    token = token or resolve_mineru_token()
    if not token:
        raise MinerUCloudError(
            "未配置 MinerU token。请在 chunk-lab/labconfig.json 填 mineru_token，"
            "或设置环境变量 MINERU_API_TOKEN")
    # model_version 用官方推荐的 vlm；配置可覆盖
    model = backend or resolve_cloud_backend() or "vlm"
    # 官方的 model_version 取值是 pipeline / vlm，而本地 backend 写作 vlm-engine，
    # 两者命名不同，这里归一化，避免把本地写法直接发上去导致参数非法
    model_version = "vlm" if "vlm" in model else ("pipeline" if "pipeline" in model else model)

    # 逐个校验文件，问题要在提交前暴露
    files = [Path(p) for p in paths]
    # 超出单批上限时明确拒绝，由调用方分批
    if len(files) > MAX_BATCH:
        raise MinerUCloudError(f"单批最多 {MAX_BATCH} 个文件，当前 {len(files)} 个")
    # 逐个检查存在性与大小
    for f in files:
        if not f.is_file():
            raise MinerUCloudError(f"文件不存在：{f}")
        if f.stat().st_size > MAX_FILE_BYTES:
            raise MinerUCloudError(f"超过官方 200MB 上限：{f.name}")

    # 申请上传链接
    payload = {
        "enable_formula": enable_formula,  # 公式识别
        "enable_table": enable_table,  # 表格识别
        "language": lang,  # 文档语言
        "model_version": model_version,  # 模型版本，vlm 为官方推荐
        # 每个文件一项；data_id 便于结果回来时对应上本地文件
        "files": [{"name": f.name, "is_ocr": is_ocr, "data_id": f.name} for f in files],
    }
    log.info(f"[cloud] 申请上传链接：{len(files)} 个文件，model_version={model_version}")
    # 发起申请
    resp = requests.post(URL_BATCH, headers=_headers(token), json=payload, timeout=60)
    data = _check(resp, "申请上传链接")
    # 取出批次号与上传地址
    batch_id = data.get("batch_id")
    urls = data.get("file_urls") or []
    # 数量对不上说明接口行为与预期不符，继续上传只会错位
    if not batch_id or len(urls) != len(files):
        raise MinerUCloudError(f"返回的上传链接数量不符：期望 {len(files)}，实际 {len(urls)}")

    # 逐个上传。官方要求 PUT 且不要带 Content-Type——
    # 带上会与预签名 URL 的签名不符，导致 403
    for f, url in zip(files, urls):
        log.info(f"[cloud] 上传 {f.name}（{f.stat().st_size // 1024} KB）")
        # 上传失败要指明是哪个文件，批量时否则无从排查
        try:
            with f.open("rb") as fh:
                r = requests.put(url, data=fh, timeout=600)
        except Exception as e:
            raise MinerUCloudError(f"上传 {f.name} 失败：{e}")
        # 非 2xx 视为失败
        if not (200 <= r.status_code < 300):
            raise MinerUCloudError(f"上传 {f.name} 失败：HTTP {r.status_code} {r.text[:150]}")

    log.info(f"[cloud] 上传完成，batch_id={batch_id}")
    # 返回批次号与文件名列表，供轮询时对应
    return batch_id, [f.name for f in files]


def poll_batch(batch_id, token=None, on_progress=None, timeout=POLL_TIMEOUT):
    """轮询批量任务，直到全部结束或超时。返回每个文件的最终状态。"""
    # 读取凭据
    token = token or resolve_mineru_token()
    # 起始时间用于超时判断
    started = time.monotonic()
    # 记录上次的状态，仅在变化时上报，避免刷屏
    last_seen = {}

    # 轮询直到全部完成或超时
    while True:
        # 超时后放弃等待，但把已知状态返回而不是抛错——部分文件可能已完成
        if time.monotonic() - started > timeout:
            log.warning(f"[cloud] 轮询超时（{timeout}s），返回当前状态")
            return list(last_seen.values())
        # 查询批次结果
        resp = requests.get(f"{URL_BATCH_RESULT}/{batch_id}",
                            headers=_headers(token), timeout=60)
        data = _check(resp, "查询批次结果")
        # 结果列表
        results = data.get("extract_result") or []
        # 逐个记录状态并上报变化
        for item in results:
            name = item.get("file_name") or item.get("data_id") or "?"
            state = item.get("state") or "unknown"
            # 状态有变化才上报，减少无谓的回调
            if last_seen.get(name, {}).get("state") != state:
                log.info(f"[cloud] {name}: {state}")
                if on_progress:
                    # 回调异常不应中断轮询
                    try:
                        on_progress(name, state, item)
                    except Exception:
                        pass
            last_seen[name] = item
        # 全部进入终态即结束；官方的终态是 done 与 failed
        if results and all((r.get("state") in ("done", "failed")) for r in results):
            return results
        # 未结束则等待下一轮
        time.sleep(POLL_INTERVAL)


def download_result(zip_url, out_dir, stem):
    """下载并解压结果 Zip，转成与本地解析同构的产物目录。

    产物结构必须与本地一致（{stem}_content_list.json / {stem}_middle.json），
    否则 chunk-lab 的语料导入、桥接读取、切分重放全都要跟着改。
    """
    # 目标目录
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 下载 zip 到内存；官方产物通常几 MB，不必落临时文件
    log.info(f"[cloud] 下载结果：{zip_url[:80]}…")
    # 下载失败要如实抛出
    try:
        r = requests.get(zip_url, timeout=600)
        r.raise_for_status()
    except Exception as e:
        raise MinerUCloudError(f"下载结果失败：{e}")

    # 解压并按本地命名落盘
    written = {}
    # 损坏的 zip 要明确报出，而不是留下半个目录
    try:
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            # 逐个成员处理
            for member in zf.namelist():
                # 目录项跳过
                if member.endswith("/"):
                    continue
                # 取文件名（去掉 zip 内的层级）
                name = Path(member).name
                # 按用途重命名为本地同构的名字；其余原样保留
                if name.endswith("content_list.json"):
                    target = out_dir / f"{stem}_content_list.json"
                elif name.endswith("middle.json"):
                    target = out_dir / f"{stem}_middle.json"
                elif name.endswith("model.json"):
                    target = out_dir / f"{stem}_model.json"
                elif name.endswith(".md"):
                    target = out_dir / f"{stem}.md"
                elif "/images/" in member or member.startswith("images/"):
                    # 图片保留原目录结构，切分器按相对路径引用它们
                    target = out_dir / "images" / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                else:
                    target = out_dir / name
                # 写出
                target.write_bytes(zf.read(member))
                written[name] = str(target)
    except zipfile.BadZipFile as e:
        raise MinerUCloudError(f"结果包损坏：{e}")

    # 没拿到 content_list 说明产物不符合预期，后续切分无从进行
    product = out_dir / f"{stem}_content_list.json"
    if not product.is_file():
        raise MinerUCloudError(
            f"结果包中没有 content_list.json（含 {len(written)} 个文件："
            f"{', '.join(list(written)[:6])}）")
    # 返回可直接用于切分的产物路径
    return product


def parse_files(paths, out_root, token=None, backend=None, lang="ch",
                on_progress=None):
    """批量云端解析的完整流程：提交 → 轮询 → 下载 → 落盘。

    返回每个文件的结果，失败项带上原因，不因个别失败中断整批。
    """
    # 提交并上传
    batch_id, names = submit_batch(paths, token=token, backend=backend, lang=lang)
    # 通知调用方已进入排队等待
    if on_progress:
        on_progress("*", "queued", {"batch_id": batch_id, "count": len(names)})
    # 轮询直到结束
    results = poll_batch(batch_id, token=token, on_progress=on_progress)

    # 按文件名索引原始路径，便于结果回来时对应
    by_name = {Path(p).name: Path(p) for p in paths}
    # 逐个处理结果
    out = []
    for item in results:
        name = item.get("file_name") or item.get("data_id") or ""
        src = by_name.get(name)
        # 失败项记录原因即可，不影响其它文件
        if item.get("state") != "done":
            out.append({"name": name, "ok": False,
                        "message": item.get("err_msg") or f"状态 {item.get('state')}"})
            continue
        # 成功项下载产物
        zip_url = item.get("full_zip_url")
        # 没有下载地址同样视为失败
        if not zip_url:
            out.append({"name": name, "ok": False, "message": "结果中没有 full_zip_url"})
            continue
        # 产物目录：与本地解析一致地按「文件名_backend」建子目录，便于区分来源
        stem = (src.stem if src else Path(name).stem)
        dest = Path(out_root) / f"{stem}_cloud" / "vlm"
        # 单个文件下载失败不影响其它
        try:
            product = download_result(zip_url, dest, stem)
            out.append({"name": name, "ok": True, "product": str(product)})
        except Exception as e:
            out.append({"name": name, "ok": False, "message": str(e)})
    # 返回逐文件结果
    return out
