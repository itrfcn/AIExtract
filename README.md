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

# 生成样例表格 / 文本
.\.venv\Scripts\python.exe data\build_samples.py

# 启动 Web 界面
.\.venv\Scripts\python.exe -m streamlit run app.py

# 或直接命令行跑（无需 Streamlit）
.\.venv\Scripts\python.exe cli.py
```

浏览器打开 http://localhost:8501 。

## 命令行用法（无需启动 Streamlit）

```powershell
# 自动扫描 data/ 下样例，一键跑通
.\.venv\Scripts\python.exe cli.py

# 指定一张表 + 多份文本（模式 A）
.\.venv\Scripts\python.exe cli.py my_table.xlsx text1.txt text2.docx text3.pdf

# 成对批量（模式 B），每张表配自己的文本
.\.venv\Scripts\python.exe cli.py --pair t1.xlsx:a.txt --pair t2.xlsx:b.txt

# 强制指定 provider（覆盖 .env 中的 LLM_PROVIDER）
.\.venv\Scripts\python.exe cli.py --provider deepseek
```

输出示例：

```
[1/5] 张伟           ok=7  uncertain=0  未提及=0
[2/5] 李娜           ok=1  uncertain=4  未提及=2
    · 姓名 = '李娜'  [uncertain(0.72)]
    · 年龄 = ''  [—]
    · 公司 = '阿里巴巴'  [uncertain(0.70)]

耗时：0.0s    行数：5
输出：E:\tiqu\outputs\sample_table_filled_20261104_140153.xlsx
写入单元格=31  跳过=0  警告=[]
原表未动，另存为新文件。
```

支持的文本源：`.txt` `.md` `.csv` `.docx` `.pdf`（仅文字层）`.xlsx`（读所有单元格文本拼起来）。

## 首次使用

1. 复制 `.env.example` 为 `.env`，填写 `LLM_API_KEY`（ollama 本地模型可留空）。
2. 「单表批量（A）」上传 `data/sample_table.xlsx`（第 1 行是合并标题，第 2 行才是表头）。
3. 确认表头行 = 2、工作表 = 人员信息。
4. 上传 `data/sample_text_1.txt` 和 `data/sample_text_2.txt`，或粘贴 `data/sample_text_multi.txt` 的内容。
5. 点击「开始抽取」。
6. 复核页黄色单元格就是低置信度项；确认后点「确认并写入表格」，导出到 `outputs/`，原文件不动。
7. 多表成对（B）可用 `data/sample_table_b.xlsx` + `data/sample_text_3.txt` 体验。

## 配置

复制 `.env.example` 为 `.env` 后修改：

| 变量 | 说明 |
| --- | --- |
| `LLM_PROVIDER` | `auto` / `openai` / `deepseek` / `ollama` |
| `LLM_BASE_URL` | OpenAI 兼容地址，留空按 provider 取默认 |
| `LLM_API_KEY` | 必需（ollama 可留空） |
| `LLM_MODEL` | 留空用默认模型（gpt-4o-mini / deepseek-chat / qwen2.5:7b） |
| `BATCH_CONCURRENCY` | 线程池并发数，默认 4（UI 可调，上限 16） |
| `LLM_QPS` | 全局请求限速 |
| `LLM_MAX_RETRIES` | 指数退避重试次数 |
| `CONFIDENCE_THRESHOLD` | 绿/黄阈值，默认 0.75 |

`auto` 模式下根据 `LLM_BASE_URL` 判断 deepseek 还是 openai；没填则默认 openai。
本地模型：`LLM_PROVIDER=ollama`、`LLM_BASE_URL=http://localhost:11434/v1`、`LLM_MODEL=qwen2.5:7b`。

## 目录结构

```
app.py                 Streamlit 单页应用（页面直接调用 pipeline）
cli.py                 命令行入口：表格 + 文本 → 抽取 → 导出，无需 Streamlit
core/config.py         .env 读取与 provider 选择/校验
pipeline/
  workbook.py          openpyxl 读表、表头定位、合并单元格检测
  schema.py            列名三级映射、FieldSpec、动态 JSON Schema
  extract.py           prompt、结构化输出、evidence 回查、置信度
  normalize.py         年龄/性别/日期/电话/列表归一化
  export.py            openpyxl 追加回填、样式继承、另存新文件
  batch.py             线程池批量调度、进度回调、失败重跑
  store.py             SQLite 任务与逐条状态（pending/running/done/failed）
  providers.py         OpenAI 兼容 Provider + 限速重试
  segment.py           文本切分为多条人物记录
  textsource.py        txt/docx/pdf(文字层)/xlsx 文本读取
data/                  样例表格与文本、build_samples.py
outputs/              导出文件（带时间戳）
tests/                 pytest：表头/Schema/归一化/批量状态/格式保留
docs/                 实现方案设计文档
```

## 当前能力

- 表头驱动：同义词词典 → rapidfuzz 模糊匹配 → LLM 兜底 → 自定义列；未知列自动参与抽取。
- 动态 JSON Schema：每个字段返回 `{value, confidence, evidence}`，关闭 `additionalProperties`。
- 证据回查：evidence 必须在原文命中，否则单元格降级为低置信度并在复核页标黄。
- 归一化：年龄整数、性别枚举、日期 ISO、手机号、多值列表；"未提及"留空不硬填。
- 批量 A：多份文本 / 一个文件多段 → 同一张表，每条记录一行。
- 批量 B：多张表各自配文本，逐表跑批次、逐表导出。
- 并发 + 限速 + 指数退避；逐条状态落 SQLite；支持只重跑失败项；结束后统一写回。
- 导出：openpyxl 追加、继承模板行样式、保留合并单元格与其他 sheet、另存带时间戳的新文件。

## 已知限制

- **扫描版 PDF 不支持**：只读取文字层；图片型 PDF 会提示"没有文字层"，不做 OCR。
- **并发上限 16**（UI 与后端均限制），并发过高可能触发服务端限流；实际吞吐还受 `LLM_QPS` 限制。
- 长文本按"单条记录"送模型；单条超过 5000 字只给提示，不做自动分段合并。
- 追加写回只支持追加到末尾；暂不支持指定起始行覆盖，也不支持 xls 旧格式。
- 表格若在数据区存在合并单元格，命中的单元格会跳过并给出警告，不会拆解合并区。
- 模板记忆（同表头复用映射）尚未实现，每次仍走一遍映射（有 Streamlit 进程内缓存）。

## 测试

```powershell
# 设置测试模式（跳过真实 API key 校验）
$env:TIQU_TEST_MODE="1"
.\.venv\Scripts\python.exe -m pytest -q
```

覆盖：表头定位（合并标题行）、导出格式与样式保留、原文件不变、合并单元格跳过、三级映射、JSON Schema、归一化、证据回查、批量状态流转与失败重跑。

## 下一步建议

1. 用真实表格 + 真实模型跑 10 组样本，统计字段准确率与编造率，再调 prompt / 阈值。
2. 复核页增加"按字段筛选低置信度""单格重抽"。
3. 表头映射模板记忆（SQLite 存 `normalized_header -> FieldSpec`），同表二次使用零调用。
4. 批量任务异步化（后台线程 + 刷新进度），避免长任务阻塞页面。
