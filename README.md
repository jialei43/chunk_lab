# chunk-lab

RagFlow MinerU 切分逻辑的离线实验室。用于持续发现切分缺陷、量化改动效果、防止回归。

## 与 ragflow 的关系

**本仓库完全独立，不属于 ragflow 的版本管理。**

- `/Users/jialei/Desktop/RagFlow/` 本身不是 git 仓库，唯一的仓库是它下面的 `ragflow/`（以及 `lzcj_web/`）。
- 本目录与它们平级，物理上不在任何一方的工作树内。在 `ragflow/` 下执行 `git status` / `git add -A` **看不到本目录的存在**——不是靠 ignore 规则挡住，是根本不在范围内。
- 本实验室对 ragflow **只读**：导入它的切分代码、读取 MinerU 产物，不写入任何文件，不安装任何依赖，`pyproject.toml` 与 `uv.lock` 保持零改动。

## 运行

```bash
./run.sh <content_list.json 路径> <原始文件名> [选项]
```

选项：

| 选项 | 说明 |
|---|---|
| `--slide` | 启用 PPTX 的 slide_mode（按幻灯片切分） |
| `--children-delimiter` | 父子分块分隔符，**对切分粒度影响极大**，见下文 |
| `--chunk-token-num` | 覆盖 token 预算 |
| `--show N` | 打印前 N 个 chunk 的正文预览 |

示例：

```bash
# PDF 双栏
./run.sh /path/to/xxx_content_list.json xxx.pdf --children-delimiter '\n'

# PPTX 按页切分
./run.sh /path/to/yyy_content_list.json yyy.pptx --slide
```

`run.sh` 固定使用 `ragflow/.venv/bin/python` 的绝对路径，不依赖当前终端激活了哪个虚拟环境，且**绝不使用 `uv run`**（避免触发 `uv.lock` 解析并静默改写环境）。

## 设计要点

离线驱动**不写任何 stub**，直接构造真实的 `MinerUParser` 并调用生产函数 `chunk_mineru_blocks`，保证离线结论与线上行为零偏差。三条依据均已在 ragflow 源码中核对：

1. `MinerUParser.__init__` 只赋值属性、不做网络调用，可无参构造；
2. `chunk_mineru_blocks` 内所有 `crop` 调用点都有 `page_images` 守卫，不提供页面图像即走"无截图"分支，与生产的 Office 路径行为一致；
3. `tenant_id` 传 `None` 时 `get_vision_model` / `get_chat_model` 直接返回 `None`，不加载 LLM、不 import `api.db`、不连数据库——因此离线结果确定可复现。

## 阶段一：保真度验证结论（已通过）

用 `tmp/pdfs/mineru_two_column_e2e_output/` 的双栏 PDF 产物离线重放，与历史生产 e2e 结果 `mineru_two_column_e2e_chunks/summary.json` 逐块比对：

- 喂给切分器的 blocks 完全一致（23 个，`text_level` 分布相同），确认产物层无偏差；
- 父块正文与生产 chunk 逐字吻合（如 `> 2 评估方法` + `[P03] 本样例采用 A4 纵向页面…`）；
- 唯一差异是面包屑分隔符（生产为换行、当前代码为 ` > `），属 summary.json 是历史产物、面包屑格式后来改过的代码演进，非离线偏差。

PPTX 路径同样验证通过（40 chunk，含 9 个表格，位置/页码/面包屑齐全）。

### 关键发现：`children_delimiter` 是切分粒度的主开关

同一份产物，仅改这一个参数，结果差异巨大：

| `children_delimiter` | chunk 数 | 平均长度 | 说明 |
|---|---|---|---|
| 空 | 14 | 127 | 每个原始段落独立成块 |
| `\n` | 14（子块） | 128 | 5 个父块，按换行拆成 14 个子块 |
| `‡`（正文中不存在） | 5 | 306 | 子拆分不切割，即父块粒度 |

机制在 [`mineru_chunker.py:1494-1498`](../ragflow/rag/app/mineru_chunker.py#L1494) 的合并边界判定：默认模式下"同标题的不同原始段落也必须独立成块"，父子模式下只有标题变化才断开。

两个必须记住的契约：

- **离线驱动必须复刻 `naive.py` 的 `children_delimiter → 正则` 转换**（含 `unicode_escape` 还原与反引号自定义分隔符），否则同一份产物会得到与生产迥异的结果；
- **父子模式下返回的全部是子块**，父块全文挂在 `mom_with_weight` 字段上。评审器必须据此区分口径，否则会把子块的短长度误判为切分过碎。

## 目录

```
labkit/      harness 代码（路径注入、离线驱动、验证脚本）
corpus/      语料：原始文件 + MinerU 缓存产物 + case.yaml（大文件不入库）
baselines/   回归基线快照（入库，用于追溯指标变化）
reports/     每轮运行输出（不入库）
```

## 后续阶段

- [x] 阶段一：离线驱动 + 缓存复用，验证保真度
- [ ] 阶段二：语料库 + 规则层检测器（截断、超长、标题孤块、表格破损、面包屑丢失、噪声残留、乱码、定位缺失、图片空壳、重复内容）
- [ ] 阶段三：快照回归 diff，改代码前后对比指标升降
- [ ] 阶段四：LLM/VLM 语义评审层（抽样）
- [ ] 阶段五：agent 循环编排与修改提案
