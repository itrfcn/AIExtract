# 文本抽取 · 自动填表（MVP）

上传一份**表头已经写好**的表格，粘贴或上传人物文本，Agent 自动抽取内容并按表头追加到新表里。
表头决定抽取字段：新增列不需要改代码。需要配置真实 API Key 才能运行。

## 快速开始

```powershell
cd e:\tiqu

# 依赖（仓库已自带 .venv，可跳过创建）
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 复制配置
copy .env.example .env
# 编辑 .env，填入 LLM_API_KEY（ollama 本地模型无需 key）

# 启动 Web 界面
.\.venv\Scripts\python.exe -m streamlit run app.py

# 或直接命令行跑（无需 Streamlit）
.\.venv\Scripts\python.exe cli.py
```

浏览器打开 http://localhost:8501 。

## 命令行用法（无需启动 Streamlit）

```powershell
# 指定一张表 + 多份文本（模式 A）
.\.venv\Scripts\python.exe cli.py my_table.xlsx text1.txt text2.docx text3.pdf

# 文件夹一键跑：脚本自动找第一张表 + 所有文本文件
.\.venv\Scripts\python.exe cli.py "C:\我的数据"
.\.venv\Scripts\python.exe cli.py --dir "C:\我的数据"

# 成对批量（模式 B），每张表配自己的文本
.\.venv\Scripts\python.exe cli.py --pair t1.xlsx:a.txt --pair t2.xlsx:b.txt

# 强制指定 provider（覆盖 .env 中的 LLM_PROVIDER）
.\.venv\Scripts\python.exe cli.py --provider deepseek
```

文件夹扫描规则：找**第一张**非填充态 `.xlsx`/`.csv`（按文件名排序）+ 目录下**所有**支持的文本文件（`.txt .md .docx .pdf(文字层) .xlsx .csv`），只扫第一层。

输出示例：

```
============================================================
  文本抽取 + 自动填表 · CLI
============================================================
  provider : gpt-6.1-sol (https://api.longxiadev.store/v1)
  concurrency=4  threshold=0.75

  文件夹扫描 → 表=人员登记表.xlsx  文本=['张伟.txt', '李娜.docx']
============================================================
  表格：人员登记表.xlsx
============================================================
  工作表：Sheet1    表头行：1    有效列：4
  表头：['姓名', '年龄', '性别', '公司']

映射结果：
  token 预估：约 1200 tokens，2 次调用
  预计费用：按 gpt-4o-mini 粗估 ~ $0.005-0.015

[1/2] 张伟           ok=4  uncertain=0  未提及=0
[2/2] 李娜           ok=1  uncertain=2  未提及=1
    · 公司 = '阿里巴巴'  [uncertain(0.70)]
    · 年龄 = ''  [—]

耗时：13.2s    行数：2
输出：E:\tiqu\outputs\人员登记表_filled_20261104_161500.xlsx
写入单元格=8  跳过=0  警告=[]
原表未动，另存为新文件。
```

支持的文本源：`.txt` `.md` `.csv` `.docx` `.pdf`（仅文字层）`.xlsx`（读所有单元格文本拼起来）。

LLM 返回全空的记录会跳过（不写入，不留空行）；单条调用失败也跳过，继续跑后续记录。

## Web 界面

### Tab A · 单表批量

两种表格来源，**二选一**：

| 方式 | 适用场景 |
|---|---|
| **上传表格** | 已有表头的 xlsx / csv，上传后自动探测表头行 |
| **手动输入表头** | 没有文件（比如公司电脑上 xlsx 被加密），直接在 text_area 里每行写一个表头 |

表头映射可在 data_editor 里改：字段类型、是否参与抽取、枚举值、描述。非同义词词典命中的字段会在"映射来源"列标明（模糊 / LLM / 自定义）。

### Tab B · 成对批量

多组「表 + 文本」配对，逐表跑、逐表导出。

### Tab · 任务历史（SQLite 持久化）

每条任务显示状态/成功/失败/未开始。选一个任务点"加载结果"，预览抽取结果后可以**直接下载 CSV（utf-8-sig，Excel 双击不乱码）或 XLSX**。

## 防编造四层防线

| 层级 | 机制 | 失败后果 |
|---|---|---|
| 1. Prompt + JSON Schema | SYSTEM_PROMPT 强制"宁漏勿错"，`strict=True` + 动态 Schema 关闭 `additionalProperties` | LLM 编不出 Schema 外字段 |
| 2. Evidence 回查 | `check_evidence` 验证 LLM 给的证据片段在原文里必须命中；中文数字自动归一（三十二 ↔ 32）；证据 < 3 字符直接拒绝 | 置信度**强制封顶 0.49** |
| 2.5. Value 反查 | `check_value_in_evidence` 验证 LLM 给的 value 必须在归一化后的 evidence 里出现（姓名/公司例外） | 置信度**强制封顶 0.49** |
| 2.6. 短证据软惩罚 | evidence 3-5 字符之间，score 扣 0.15 | 推到阈值边缘，容易落入复核 |
| 3. 置信度打分 | `score = 0.5*llm_conf + 0.3*evidence_ok + 0.2*normalized_ok - 短证据惩罚` | 三重门（evidence_ok + value_ok + normalized_ok）**全过**才算 ok |
| 4. 归一化 | 年龄 0-120 int、性别枚举、日期可解析、电话 11 位 | normalized_ok=False → 再扣 0.2 |
| 5. 阈值过滤 | score ≥ 0.75（默认）才 ok | 其余标黄进复核页 |

阈值在 Streamlit 侧边栏 slider 或 `.env` 的 `CONFIDENCE_THRESHOLD` 里调。

## 配置

复制 `.env.example` 为 `.env` 后修改：

| 变量 | 说明 |
| --- | --- |
| `LLM_PROVIDER` | `openai` / `deepseek` / `ollama`（无 `auto`，必须显式选一个） |
| `LLM_BASE_URL` | OpenAI 兼容地址 |
| `LLM_API_KEY` | **必需**（ollama 本地模型可留空） |
| `LLM_MODEL` | 留空用默认模型（openai: gpt-4o-mini / deepseek: deepseek-chat / ollama: qwen2.5:7b） |
| `BATCH_CONCURRENCY` | 线程池并发数，默认 4（UI 可调，上限 16） |
| `LLM_QPS` | 全局请求限速，默认 3 |
| `LLM_MAX_RETRIES` | 指数退避 + 抖动重试次数，默认 3 |
| `LLM_TIMEOUT` | 单次调用超时秒，默认 60 |
| `CONFIDENCE_THRESHOLD` | 绿/黄阈值，默认 0.75 |
| `DATA_DIR` | SQLite 和上传文件目录 |
| `OUTPUT_DIR` | 导出目录 |

本地 Ollama 示例：
```
LLM_PROVIDER=ollama
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen2.5:7b
# LLM_API_KEY 可留空
```

## 目录结构

```
app.py                 Streamlit 单页：单表批量(A)/成对批量(B)/任务历史
cli.py                 命令行入口：文件、文件夹、--pair 三种模式
core/config.py         .env 读取 + provider 校验 + 目录创建
pipeline/
  workbook.py          openpyxl 读表、表头定位、合并单元格检测、csv 兼容
  schema.py            列名三级映射（词典→模糊→LLM→自定义）、FieldSpec、JSON Schema
  extract.py           SYSTEM_PROMPT、chat_json 调用、evidence 回查、value 反查、中文数字归一、置信度打分
  normalize.py         年龄/性别/日期/电话/学历/列表归一化，export 专用格式化
  export.py            openpyxl 追加回填、样式继承、合并单元格跳过、另存新文件；export_new_table 新建表
  batch.py             线程池批量、进度回调、失败重跑、状态更新
  store.py             SQLite jobs/items（WAL），任务历史持久化，双检自动建表
  providers.py         BaseProvider、OpenAI 兼容（strict=True 自动降级、指数退避重试）、全局 RateLimiter
  segment.py           文本切分：空行分隔/编号前缀/「姓名：」提示
  textsource.py        txt/docx/pdf(文字层)/xlsx/csv/md 读取，编码自适应
data/                  样例表和文本（可放自定义文件）
outputs/               导出文件（带时间戳）
tests/                 pytest：5 文件 19 条用例
docs/                  实现方案设计文档
```

## 当前能力

- 表头驱动：同义词词典 → rapidfuzz 模糊 → LLM 兜底 → 自定义列；未知列自动参与抽取，不丢任何字段。
- 动态 JSON Schema：每个字段返回 `{value, confidence, evidence}`，关闭 `additionalProperties`。
- 四层防编造（见上）：evidence 回查（含中文数字归一）+ value 反查 + 短证据惩罚 + 归一化校验 + 阈值过滤。
- 归一化：年龄整数、性别枚举、日期 ISO、手机号、多值列表；"未提及"留空不硬填。
- 批量 A：多份文本 / 一个文件多段 → 同一张表，每条记录一行；支持**文件夹一键跑**。
- 批量 B：多张表各自配文本，逐表跑批次、逐表导出。
- 并发 + 限速 + 指数退避；逐条状态落 SQLite；支持只重跑失败项；结束后统一写回。
- 导出：openpyxl 追加、继承模板行样式、保留合并单元格与其他 sheet、另存带时间戳的新文件；不覆盖原表。
- Streamlit 手动输入表头模式（无 xlsx 也能跑）。
- 任务历史 tab 预览 + 下载 CSV/XLSX。
- CLI 跳过全空记录和失败记录，不留空行。

## 已知限制

- **扫描版 PDF 不支持**：只读取文字层；图片型 PDF 会提示"没有文字层"，不做 OCR。
- **并发上限 16**（UI 与后端均限制），并发过高可能触发服务端限流；实际吞吐还受 `LLM_QPS` 限制。
- 长文本按"单条记录"送模型；单条超过 5000 字只给提示，不做自动分段合并。
- 追加写回只支持追加到末尾；暂不支持指定起始行覆盖，也不支持 `.xls` 旧格式。
- 表格若在数据区存在合并单元格，命中的单元格会跳过并给出警告，不会拆解合并区。
- 文件夹扫描**只扫第一层**，不递归子目录；多张表时按文件名排序取第一张。
- 模板记忆（同表头复用映射）尚未实现，每次仍走一遍映射（Streamlit 进程内有缓存）。
- CLI 跳过失败/全空记录后，导出文件行数 < 输入记录数；无「失败清单 CSV」。

## 测试

```powershell
# 设置测试模式（跳过真实 API key 校验）
$env:TIQU_TEST_MODE="1"
.\.venv\Scripts\python.exe -m pytest -q
```

覆盖：表头定位（合并标题行）、导出格式与样式保留、原文件不变、合并单元格跳过、三级映射、JSON Schema、归一化、证据回查 + value 反查 + 中文数字归一 + 短证据长度限制、批量状态流转与失败重跑。

## 下一步建议

1. 用真实表格 + 真实模型跑 10 组样本，统计字段准确率与编造率，再调 prompt / 阈值。
2. 复核页增加「按字段筛选低置信度」「单格重抽」。
3. 表头映射模板记忆（SQLite 存 `normalized_header → FieldSpec`），同表二次使用零调用。
4. 批量任务异步化（后台线程 + 刷新进度），避免长任务阻塞页面。
5. 文件夹模式支持**多张表配对多份文本**（自动按文本文件名中的人名匹配目标表）。
6. CLI 增加 `--failed-only`，从 SQLite 里读上次任务的失败条目重跑。
