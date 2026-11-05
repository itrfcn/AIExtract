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

# 调并发和阈值（CLI 参数优先级最高，覆盖 .env）
.\.venv\Scripts\python.exe cli.py table.xlsx texts\*.txt --concurrency 8 --threshold 0.6

# 文本切分：默认按 PDF 页拆（一页一条）；docx/txt/图片没有分页符，等于一个文件一条
.\.venv\Scripts\python.exe cli.py --dir "C:\我的数据"                    # 默认（PDF 按页）
.\.venv\Scripts\python.exe cli.py --dir "C:\我的数据" --split none         # 整个文件算一条（多页单份简历）
.\.venv\Scripts\python.exe cli.py --dir "C:\我的数据" --split auto         # 自动判断（单人简历不拆）
.\.venv\Scripts\python.exe cli.py --dir "C:\我的数据" --split blank        # 按空行拆分
.\.venv\Scripts\python.exe cli.py --dir "C:\我的数据" --split name         # 按姓名标签拆分

# 强制指定 provider（覆盖 .env 中的 LLM_PROVIDER）
.\.venv\Scripts\python.exe cli.py --provider deepseek
```

文件夹扫描规则：找**第一张**非填充态 `.xlsx`/`.csv`（按文件名排序）+ 目录下**所有**支持的文本文件（`.txt .md .doc .docx .pdf(文字层) .xlsx .csv`），只扫第一层。

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

支持的文本源：`.txt` `.md` `.csv` `.doc`（Word 97-2003，纯 Python 解析）`.docx` `.pdf`（文字层直读，扫描页走 OCR）`.xlsx`（读所有单元格文本拼起来）`.png` `.jpg` `.jpeg` `.bmp` `.tif` `.webp`（OCR）。扫描件默认用云端视觉模型转写（按阅读顺序、先左栏后右栏），失败自动回退本地 rapidocr。

LLM 返回全空的记录会跳过（不写入，不留空行）；单条调用失败也跳过，继续跑后续记录。

## Web 界面

### 后台异步执行

Tab A / Tab B 的抽取任务都在**后台线程**跑：点"开始抽取"后页面立即解锁，每 3 秒自动刷新进度（从 SQLite 读状态）。任务跑完自动进复核页。**关页面/切 tab/重启 Streamlit 都不丢**——任务历史 tab 里还能看到进度和下载。

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
| 2.7. 候选人身份校验 | 姓名列以原文「姓 名：」标签为准；与标签不符按标签校正；evidence 指向推荐顾问/联系人时直接丢弃 | 推荐人不会被写成候选人 |
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
| `OCR_BACKEND` | `vision`（默认，云端视觉模型读页图）/ `rapidocr`（本地离线） |
| `VISION_MODEL` | 视觉模型名，留空复用 `LLM_MODEL` |
| `VISION_CONCURRENCY` | 多页并发识别数，默认 4（上限 8） |
| `VISION_MAX_SIDE` | 送图前缩放的最长边，默认 1600（越小越快越省 token） |
| `VISION_FALLBACK_LOCAL` | `vision` 失败时回退本地 `rapidocr`，默认 1 |
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
  segment.py           文本切分：不拆分（默认）/ 自动判断 / 按空行 / 按姓名标签
  ocr.py               识别入口：vision（云端视觉模型）/ rapidocr（本地）后端切换
  vision.py            云端视觉模型读页图 → 按阅读顺序转写纯文本（多页并发）
  textsource.py        txt/doc/docx/pdf(文字层+扫描件 OCR)/图片/xlsx/csv/md 读取，编码自适应
data/                  样例表和文本（可放自定义文件）
outputs/               导出文件（带时间戳）
tests/                 pytest：9 文件 61 条用例
docs/                  实现方案设计文档
```

## 当前能力

- 表头驱动：同义词词典 → rapidfuzz 模糊 → LLM 兜底 → 自定义列；未知列自动参与抽取，不丢任何字段。
- 动态 JSON Schema：每个字段返回 `{value, confidence, evidence}`，关闭 `additionalProperties`。
- 四层防编造（见上）：evidence 回查（含中文数字归一）+ value 反查 + 短证据惩罚 + 归一化校验 + 阈值过滤。
- 归一化：年龄整数、性别枚举、日期 ISO、手机号、多值列表；"未提及"留空不硬填。
- 年龄是唯一允许的派生字段：原文只给出生年月时按当前年份算周岁（结果标"由出生年月推算（请核对）"，置信度封顶 0.8）；其他字段禁止推算。
- 批量 A：多份文本 / 一个文件多段 → 同一张表，每条记录一行；支持**文件夹一键跑**。
- 批量 B：多张表各自配文本，逐表跑批次、逐表导出。
- 并发 + 限速 + 指数退避；逐条状态落 SQLite；支持只重跑失败项；结束后统一写回。
- 记录切分默认**按 PDF 页拆**（PDF 合集一页一条；docx/txt/图片没有分页符，仍是一个文件一条）；也可手动选不拆分 / 自动判断 / 按空行 / 按「姓名」标签，Web 端单表/成对批量都有开关，CLI 用 `--split page|none|auto|blank|name`。
- 姓名列以原文「姓 名：」标签为准；没有标签时用简历开头单独一行的姓名作提示，推荐顾问/联系人/HR 等第三方信息不会写进候选人字段。
- 简历不一定写「姓名」两个字：自动判断靠分节标题（工作经历/教育背景/自我评价…）和手机号数量识别单人或多人。
- 导出：openpyxl 追加、继承模板行样式、保留合并单元格与其他 sheet、另存带时间戳的新文件；不覆盖原表。
- Streamlit 手动输入表头模式（无 xlsx 也能跑）。
- 任务历史 tab 预览 + 下载 CSV/XLSX。
- CLI 跳过全空记录和失败记录，不留空行；支持 `--concurrency` / `--threshold` 参数。
- 后台异步：Tab A/B 任务在线程池跑，页面每 3 秒刷新进度，关页面不中断。

## 已知限制

- **扫描版 PDF / 图片走 OCR**：默认 `OCR_BACKEND=vision`，用云端视觉模型把页图按阅读顺序转写成文本（双栏/设计类简历比本地 OCR 准），多页并发；失败且 `VISION_FALLBACK_LOCAL=1` 时回退本地 `rapidocr`。该步骤会把简历图片发往 `LLM_BASE_URL`，请确认合规。纯本地可设 `OCR_BACKEND=rapidocr`（离线但 CPU 约 8–10 秒/页）。
- **Word 文档**：`.docx` 直读；`.doc`（Word 97-2003）用纯 Python 解析 OLE 二进制，Word 6/95 及被加密的 `.doc` 会提示另存为 `.docx`。
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

覆盖：表头定位（合并标题行）、导出格式与样式保留、原文件不变、合并单元格跳过、三级映射、JSON Schema、归一化、证据回查 + value 反查 + 中文数字归一 + 短证据长度限制、年龄由出生年月推算（唯一允许的派生）、批量状态流转与失败重跑、docx/doc 文本读取与降级提示、扫描件 OCR 回退与缺依赖提示、vision 后端并发/顺序/失败回退、单人简历整份一条 + 多人切分、姓名标签校正与推荐人丢弃。

## 下一步建议

1. 用真实表格 + 真实模型跑 10 组样本，统计字段准确率与编造率，再调 prompt / 阈值。
2. 复核页增加「按字段筛选低置信度」「单格重抽」。
3. 表头映射模板记忆（SQLite 存 `normalized_header → FieldSpec`），同表二次使用零调用。
4. 文件夹模式支持**多张表配对多份文本**（自动按文本文件名中的人名匹配目标表）。
5. CLI 增加 `--retry <job_id>`，从 SQLite 里读上次任务的失败条目重跑。
6. 失败/全空记录导出为失败清单 CSV，便于人工补录。
7. OCR 结果纠错（表格线/多栏简历的顺序还原）。
