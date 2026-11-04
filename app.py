"""Streamlit 单页应用：上传表格 → 表头映射 → 批量抽取 → 复核 → 导出。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pandas as pd
import streamlit as st

from core.config import settings
from pipeline import store
from pipeline.batch import retry_failed, run_batch
from pipeline.export import export_new_table, export_rows
from pipeline.extract import estimate_tokens
from pipeline.normalize import normalize_for_export
from pipeline.providers import get_provider
from pipeline.schema import (
    FieldSpec,
    build_json_schema,
    map_headers,
    sanitize_key,
)
from pipeline.segment import split_records
from pipeline.textsource import read_text_file
from pipeline.workbook import inspect_table

st.set_page_config(page_title="文本抽取 · 自动填表", page_icon="🗂️", layout="wide")

UPLOAD_DIR = settings.data_dir / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ORIGIN_LABEL = {
    "dict": "同义词词典",
    "fuzzy": "模糊匹配",
    "llm": "LLM 兜底",
    "custom": "自定义列",
    "user": "手动设置",
}
TYPE_OPTIONS = ["string", "integer", "number", "date", "enum", "list"]


# ---------------------------------------------------------------- 基础工具


def save_upload(uploaded) -> Path:
    digest = hashlib.md5(uploaded.getvalue()).hexdigest()[:10]
    safe_name = re.sub(r"[^\w.\-\u4e00-\u9fa5]", "_", uploaded.name)
    path = UPLOAD_DIR / f"{digest}_{safe_name}"
    if not path.exists():
        path.write_bytes(uploaded.getvalue())
    return path


@st.cache_data(show_spinner="正在解析表格...")
def cached_inspect(path_str: str, sheet: str | None, header_row: int | None):
    return inspect_table(path_str, sheet, header_row)


@st.cache_data(show_spinner="正在生成字段映射...")
def cached_specs(path_str: str, sheet: str | None, header_row: int | None, use_llm: bool, provider_sig: str):
    info = inspect_table(path_str, sheet, header_row)
    samples = {c.index: c.sample_values for c in info.headers}
    provider = get_provider() if use_llm else None
    return map_headers([c.header for c in info.headers], samples=samples, use_llm=use_llm, provider=provider)


def specs_to_df(specs: list[FieldSpec]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "序号": s.column_index + 1,
            "原列名": s.header,
            "字段key": s.key,
            "类型": s.type,
            "必填": s.required,
            "枚举值(逗号分隔)": "、".join(s.enum_values or []),
            "字段说明": s.description,
            "抽取": s.enabled,
            "映射来源": ORIGIN_LABEL.get(s.origin, s.origin),
        }
        for s in specs
    ])


def df_to_specs(df: pd.DataFrame, base_specs: list[FieldSpec]) -> list[FieldSpec]:
    result: list[FieldSpec] = []
    used: dict[str, int] = {}
    for pos, (_, row) in enumerate(df.iterrows()):
        base = base_specs[pos]
        key = sanitize_key(str(row.get("字段key") or base.key))
        if key in used:
            used[key] += 1
            key = f"{key}_{used[key]}"
        else:
            used[key] = 1
        enum_text = str(row.get("枚举值(逗号分隔)") or "")
        enum_values = [v.strip() for v in re.split(r"[,，、;；|]", enum_text) if v.strip()] or None
        spec = base.model_copy(update={
            "key": key,
            "type": row.get("类型") or base.type,
            "required": bool(row.get("必填")),
            "enum_values": enum_values,
            "description": str(row.get("字段说明") or base.description),
            "enabled": bool(row.get("抽取")),
            "origin": "user",
        })
        result.append(spec)
    return result


def render_spec_editor(specs: list[FieldSpec], key: str) -> list[FieldSpec]:
    df = specs_to_df(specs)
    edited = st.data_editor(
        df,
        key=key,
        hide_index=True,
        num_rows="fixed",
        width="stretch",
        disabled=["序号", "原列名", "映射来源"],
        column_config={
            "序号": st.column_config.NumberColumn(width=60),
            "原列名": st.column_config.TextColumn(width=110),
            "字段key": st.column_config.TextColumn(width=110, help="给模型的字段名，可用中文或英文"),
            "类型": st.column_config.SelectboxColumn(width=90, options=TYPE_OPTIONS, required=True),
            "必填": st.column_config.CheckboxColumn(width=60),
            "枚举值(逗号分隔)": st.column_config.TextColumn(width=160),
            "字段说明": st.column_config.TextColumn(width=280),
            "抽取": st.column_config.CheckboxColumn(width=60, help="取消勾选则忽略该列"),
            "映射来源": st.column_config.TextColumn(width=100),
        },
    )
    return df_to_specs(edited, specs)


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "、".join(str(v) for v in value)
    return str(value)


def _spec_labels(specs: list[FieldSpec]) -> list[str]:
    labels: list[str] = []
    for spec in specs:
        label = spec.header
        while label in labels:
            label = label + "*"
        labels.append(label)
    return labels


def _records_from_uploads(uploaded_files, pasted_text: str, mode: str):
    records = []
    warnings: list[str] = []
    for uploaded in uploaded_files or []:
        path = save_upload(uploaded)
        text, file_warnings = read_text_file(path)
        warnings.extend(f"{uploaded.name}：{w}" for w in file_warnings)
        records.extend(split_records(text, mode, source_name=uploaded.name))
    if (pasted_text or "").strip():
        records.extend(split_records(pasted_text, mode, source_name="粘贴文本"))
    return records, warnings


def start_job(mode: str, table_path: Path, sheet: str, header_row: int,
              specs: list[FieldSpec], records, concurrency: int, threshold: float,
              meta: dict | None = None) -> tuple[str, dict]:
    job_id = store.create_job(mode, str(table_path), sheet, header_row, specs, meta=meta)
    store.add_items(job_id, [
        {"source_name": r.source_name, "record_id": r.record_id, "name_hint": r.name_hint, "text": r.text}
        for r in records
    ])
    progress = st.progress(0.0, text="准备抽取...")
    status_line = st.empty()

    def callback(done: int, total: int, outcome: dict) -> None:
        progress.progress(min(1.0, done / max(1, total)), text=f"已完成 {done}/{total} 条")
        status_line.caption(
            f"最近：{outcome.get('source_name') or '文本'} · "
            f"{outcome.get('status')} · {outcome.get('name_hint') or outcome.get('record_id') or ''}"
        )

    stats = run_batch(job_id, specs, get_provider(), concurrency=concurrency,
                      threshold=threshold, progress_cb=callback)
    progress.progress(1.0, text=f"抽取完成：成功 {stats['done']} 条，失败 {stats['failed']} 条")
    return job_id, stats


def render_job_review(job_id: str, specs: list[FieldSpec], key_prefix: str,
                      concurrency: int, threshold: float,
                      is_manual: bool = False, manual_headers: list[str] | None = None) -> None:
    job = store.get_job(job_id)
    items = store.fetch_items(job_id)
    if not job or not items:
        st.info("该任务还没有结果")
        return

    done_items = [it for it in items if it["status"] == "done"]
    failed_items = [it for it in items if it["status"] == "failed"]
    pending_items = [it for it in items if it["status"] in ("pending", "running")]

    cols = st.columns(4)
    cols[0].metric("总记录", len(items))
    cols[1].metric("已完成", len(done_items))
    cols[2].metric("失败", len(failed_items))
    cols[3].metric("未开始", len(pending_items))

    if failed_items:
        with st.expander(f"❌ {len(failed_items)} 条失败（可只重跑失败项）", expanded=len(done_items) == 0):
            st.dataframe(
                pd.DataFrame([{"来源": it["source_name"], "记录": it["record_id"],
                               "姓名提示": it["name_hint"], "错误": it["error"]} for it in failed_items]),
                width="stretch", hide_index=True,
            )
        if st.button("只重跑失败项", key=f"{key_prefix}_retry"):
            progress = st.progress(0.0, text="重跑失败项...")

            def cb(done, total, outcome):
                progress.progress(min(1.0, done / max(1, total)),
                                  text=f"重跑 {done}/{total} · {outcome.get('status')}")

            retry_failed(job_id, specs, get_provider(), concurrency=concurrency,
                         threshold=threshold, progress_cb=cb)
            st.rerun()

    if not done_items:
        return

    labels = _spec_labels(specs)
    enabled_specs = [s for s in specs if s.enabled]
    enabled_labels = [labels[i] for i, s in enumerate(specs) if s.enabled]

    display_rows = []
    status_rows = []
    for order, item in enumerate(done_items, 1):
        result = item.get("result") or {}
        row = {"记录": order, "姓名提示": item.get("name_hint") or "", "来源": item.get("source_name") or ""}
        status_row = {}
        review_fields = []
        for spec, label in zip(enabled_specs, enabled_labels):
            cell = result.get(spec.key) or {}
            row[label] = _cell_text(cell.get("value"))
            status_row[label] = cell.get("status") or "not_mentioned"
            if cell.get("status") == "uncertain":
                review_fields.append(spec.header)
        row["待复核字段"] = "、".join(review_fields)
        display_rows.append(row)
        status_rows.append(status_row)

    df = pd.DataFrame(display_rows)
    css = pd.DataFrame("", index=df.index, columns=df.columns)
    for i, status_row in enumerate(status_rows):
        for label, status in status_row.items():
            if status == "uncertain":
                css.at[i, label] = "background-color: #fff3cd"
            elif status == "ok":
                css.at[i, label] = ""
        if display_rows[i]["待复核字段"]:
            css.at[i, "待复核字段"] = "color: #9a6700; font-weight: 600"

    st.markdown("**复核结果**：黄色单元格 = 低置信度/证据不足，可直接修改后导出。")
    edited = st.data_editor(
        df.style.apply(lambda _: css, axis=None),
        key=f"{key_prefix}_review",
        hide_index=True,
        num_rows="fixed",
        width="stretch",
        disabled=["记录", "姓名提示", "来源", "待复核字段"],
    )

    with st.expander("查看原文与字段证据（抽查用）", expanded=False):
        pick = st.selectbox(
            "选择记录", options=list(range(len(done_items))),
            format_func=lambda i: f"{i + 1}. {done_items[i].get('name_hint') or done_items[i].get('source_name') or ''}",
            key=f"{key_prefix}_evidence_pick",
        )
        item = done_items[pick]
        st.text_area("原文", item["text"], height=150, disabled=True, key=f"{key_prefix}_source_{pick}")
        evidence_rows = []
        for spec in enabled_specs:
            cell = (item.get("result") or {}).get(spec.key) or {}
            evidence_rows.append({
                "字段": spec.header,
                "值": _cell_text(cell.get("value")),
                "状态": cell.get("status"),
                "置信度": cell.get("confidence"),
                "证据": cell.get("evidence") or "",
                "证据命中": cell.get("evidence_ok"),
            })
        st.dataframe(pd.DataFrame(evidence_rows), width="stretch", hide_index=True)

    if st.button("确认并写入表格（另存新文件）", key=f"{key_prefix}_export", type="primary"):
        final_rows = []
        for _, row in edited.iterrows():
            record = {}
            for spec, label in zip(enabled_specs, enabled_labels):
                value = row[label]
                if isinstance(value, float) and pd.isna(value):
                    value = None
                record[spec.key] = normalize_for_export(value, spec)
            final_rows.append(record)
        try:
            if is_manual and manual_headers:
                result = export_new_table(
                    manual_headers, specs, final_rows, stem="手动抽取结果",
                )
            else:
                result = export_rows(job["table_path"], job["sheet"], job["header_row"], specs, final_rows)
            st.session_state[f"{key_prefix}_export_path"] = str(result.path)
            st.success(f"已导出：{result.path}（写入 {result.written_rows} 行 / {result.written_cells} 个单元格）")
            if result.warnings:
                with st.expander("导出警告"):
                    for warning in result.warnings:
                        st.warning(warning)
        except Exception as exc:  # noqa: BLE001
            st.error(f"导出失败：{exc}")

    export_path = st.session_state.get(f"{key_prefix}_export_path")
    if export_path and Path(export_path).exists():
        path = Path(export_path)
        st.download_button(
            "⬇️ 下载导出文件",
            data=path.read_bytes(),
            file_name=path.name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if path.suffix.lower() != ".csv" else "text/csv",
            key=f"{key_prefix}_download",
        )


# ---------------------------------------------------------------- 侧边栏

with st.sidebar:
    st.header("运行配置")
    st.success(f"模型：{settings.provider_label}")
    # st.caption(f"Base URL：{settings.base_url or '默认'}")
    concurrency = st.slider("并发数", 1, 16, settings.concurrency)
    threshold = st.slider("置信度阈值", 0.30, 0.95, float(settings.confidence_threshold), 0.05)
    st.divider()
    st.caption(f"数据库：{settings.db_path}")
    st.caption(f"输出目录：{settings.output_dir}")
    # if st.checkbox("显示调试信息", value=False):
    #     st.code(
    #         f"provider={settings.provider}\nmodel={settings.model}\nbase_url={settings.base_url}"
    #     )

st.title("文本抽取 · 自动填表")
st.caption("上传表格 → 确认表头与字段映射 → 批量抽取 → 复核低置信度 → 追加导出（不覆盖原文件）")

tab_a, tab_b, tab_history = st.tabs(["单表批量（A）", "多表成对（B）", "任务历史"])

# ---------------------------------------------------------------- Tab A

with tab_a:
    source_mode = st.segmented_control(
        "表格来源", ["上传表格", "手动输入表头"],
        default="上传表格", key="a_source",
    )

    specs = []
    table_path = None
    info = None
    headers_list: list[str] = []
    is_manual = source_mode == "手动输入表头"

    if is_manual:
        st.subheader("1. 输入表头")
        st.caption("一行一个列名，可用中文或英文；支持括号单位如「年龄（岁）」，模糊匹配会自动处理。")
        manual_text = st.text_area(
            "表头（每行一个）",
            value="姓名\n年龄\n性别\n公司\n职位\n手机",
            height=150, key="a_manual_headers",
        )
        headers_list = [line.strip() for line in manual_text.splitlines() if line.strip()]
        if not headers_list:
            st.info("至少输入 1 个表头。")
        else:
            st.success(f"已输入 {len(headers_list)} 个表头：{headers_list}")
            provider = get_provider()
            specs = map_headers(headers_list, use_llm=True, provider=provider)
            specs = render_spec_editor(specs, "a_specs_editor")
            enabled_specs = [s for s in specs if s.enabled]

            if not enabled_specs:
                st.error("至少勾选一列参与抽取")
            else:
                with st.expander("查看动态 JSON Schema（调试用）", expanded=False):
                    st.json(build_json_schema(enabled_specs))

            # 手动模式：虚拟路径（供 job 记录 & history 展示）
            table_path = settings.data_dir / f"manual_{Path.cwd().name}.xlsx"

    else:
        st.subheader("1. 上传表格")
        table_file = st.file_uploader("选择表头已写好的表格", type=["xlsx", "xlsm", "csv"], key="a_table")
        if not table_file:
            st.info("请上传一份已写好表头的 xlsx 或 csv。可先用 data/ 目录下的样例表体验。")
        else:
            table_path = save_upload(table_file)
            try:
                base_info = cached_inspect(str(table_path), None, None)
            except Exception as exc:  # noqa: BLE001
                st.error(f"表格无法解析：{exc}")
                st.stop()

            col1, col2 = st.columns([2, 1])
            with col1:
                sheet = st.selectbox("工作表", base_info.sheet_names, index=0, key="a_sheet")
            with col2:
                header_row = st.number_input("表头行", min_value=1, max_value=100,
                                             value=min(100, int(base_info.header_row)), step=1, key="a_header_row")
            info = cached_inspect(str(table_path), sheet, int(header_row))

            for warning in info.warnings:
                st.warning(warning)

            preview_headers = [c.header for c in info.headers]
            preview_rows = [[row[c.index] if c.index < len(row) else "" for c in info.headers] for row in info.preview]
            with st.expander("表头与数据预览", expanded=len(info.preview) == 0):
                if preview_rows:
                    st.dataframe(pd.DataFrame(preview_rows, columns=preview_headers), width="stretch", hide_index=True)
                else:
                    st.info("该表只有表头，没有已有数据行；抽取结果将追加到表头之下。")
                st.caption(f"识别到 {len(info.headers)} 列 / {info.row_count} 行已有数据")

            st.subheader("2. 字段映射（可直接修改）")
            provider = get_provider()
            specs = cached_specs(str(table_path), sheet, int(header_row), True, provider.signature)
            specs = render_spec_editor(specs, "a_specs_editor")

            enabled_specs = [s for s in specs if s.enabled]
            if not enabled_specs:
                st.error("至少勾选一列参与抽取")
            else:
                with st.expander("查看动态 JSON Schema（调试用）", expanded=False):
                    st.json(build_json_schema(enabled_specs))
                st.caption("列名未命中词典时会走模糊匹配；未识别列自动作为自定义字段参与抽取，无需改代码。")

    st.subheader("3. 添加文本")
    text_files = st.file_uploader(
        "上传文本（可多选：txt / docx / pdf / xlsx）",
        type=["txt", "md", "docx", "pdf", "xlsx", "xlsm"],
        accept_multiple_files=True,
        key="a_texts",
    )
    pasted = st.text_area("或直接粘贴文本（多段用空行分隔）", height=160, key="a_pasted")
    mode_label = st.radio("记录切分", ["自动切分", "整段视为一条"], horizontal=True, key="a_mode")
    split_mode = "auto" if mode_label == "自动切分" else "single"

    if table_path and specs:
        records, text_warnings = _records_from_uploads(text_files, pasted, split_mode)
        for warning in text_warnings:
            st.warning(warning)
        if records:
            estimate = estimate_tokens([r.text for r in records])
            cols = st.columns(3)
            cols[0].metric("待抽取记录", len(records))
            cols[1].metric("总字符数", estimate["chars"])
            cols[2].metric("预估 token", f"{estimate['total_tokens']:,}")
            st.caption(estimate["rough_cost_note"])
            with st.expander("记录预览", expanded=False):
                st.dataframe(
                    pd.DataFrame([
                        {"序号": i + 1, "来源": r.source_name, "姓名提示": r.name_hint or "",
                         "字数": len(r.text), "内容": r.text[:100].replace("\n", " ")}
                        for i, r in enumerate(records)
                    ]),
                    width="stretch", hide_index=True,
                )
        else:
            st.info("上传文件或粘贴文本后，这里会显示切分结果。")

        header_row_for_job = 1 if is_manual else (int(info.header_row) if info else 1)
        sheet_for_job = "抽取结果" if is_manual else (info.sheet if info else "Sheet1")

        if records and enabled_specs and st.button("开始抽取", type="primary", key="a_run"):
            job_id, stats = start_job(
                "A", table_path, sheet_for_job, header_row_for_job, specs, records,
                concurrency, threshold,
                meta={"is_manual": is_manual, "headers": headers_list if is_manual else None},
            )
            st.session_state["a_job_id"] = job_id
            st.session_state["a_is_manual"] = is_manual
            st.session_state["a_headers"] = headers_list if is_manual else None
            st.success(f"抽取完成：成功 {stats['done']} 条，失败 {stats['failed']} 条")

        job_id = st.session_state.get("a_job_id")
        if job_id:
            st.divider()
            st.subheader("4. 复核与导出")
            render_job_review(
                job_id, specs, "a", concurrency, threshold,
                is_manual=is_manual, manual_headers=headers_list if is_manual else None,
            )

# ---------------------------------------------------------------- Tab B

with tab_b:
    st.subheader("多张表各自配文本，批量成对抽取")
    b_tables = st.file_uploader("上传多张表格", type=["xlsx", "xlsm", "csv"],
                                accept_multiple_files=True, key="b_tables")
    b_texts = st.file_uploader("上传多份文本", type=["txt", "md", "docx", "pdf", "xlsx", "xlsm"],
                               accept_multiple_files=True, key="b_texts")
    b_split = st.radio("文本切分", ["自动切分", "整段视为一条"], horizontal=True, key="b_mode")
    split_mode_b = "auto" if b_split == "自动切分" else "single"

    if not b_tables or not b_texts:
        st.info("需要至少 1 张表和 1 份文本。每张表会按顺序匹配一份文本，可手动改对应关系。")
    else:
        pairs = []
        for idx, table in enumerate(b_tables):
            default = min(idx, len(b_texts) - 1)
            chosen = st.selectbox(
                f"「{table.name}」对应文本",
                options=list(range(len(b_texts))),
                index=default,
                format_func=lambda i: f"{i + 1}. {b_texts[i].name}",
                key=f"b_pair_{idx}",
            )
            pairs.append((table, b_texts[chosen]))

        if st.button("批量成对抽取", type="primary", key="b_run"):
            b_results = []
            overall = st.progress(0.0, text="准备批量任务...")
            for i, (table, text_source) in enumerate(pairs):
                table_path = save_upload(table)
                try:
                    info = cached_inspect(str(table_path), None, None)
                    text_path = save_upload(text_source)
                    text, warnings = read_text_file(text_path)
                    records = split_records(text, split_mode_b, source_name=text_source.name)
                    specs = cached_specs(str(table_path), info.sheet, int(info.header_row), True,
                                         get_provider().signature)
                    if not records:
                        b_results.append({"name": table.name, "job_id": None, "specs": [],
                                          "error": "文本为空或未切出记录"})
                        continue
                    job_id, stats = start_job(
                        "B", table_path, info.sheet, int(info.header_row), specs, records,
                        concurrency, threshold,
                        meta={"text_source": text_source.name, "warnings": warnings},
                    )
                    b_results.append({"name": table.name, "job_id": job_id, "specs": specs, "error": None})
                except Exception as exc:  # noqa: BLE001
                    b_results.append({"name": table.name, "job_id": None, "specs": [], "error": str(exc)})
                overall.progress((i + 1) / len(pairs), text=f"已完成 {i + 1}/{len(pairs)} 张表")
            st.session_state["b_results"] = b_results
            overall.progress(1.0, text="批量抽取结束")

        b_results = st.session_state.get("b_results")
        if b_results:
            st.divider()
            st.subheader("批量结果复核")
            for index, result in enumerate(b_results):
                if result.get("error"):
                    st.error(f"{result['name']}：{result['error']}")
                    continue
                stats = store.job_stats(result["job_id"])
                with st.expander(
                    f"{result['name']} · 成功 {stats['done']} / 失败 {stats['failed']} / 共 {stats['total']}",
                    expanded=(index == 0),
                ):
                    render_job_review(result["job_id"], result["specs"], f"b{index}", concurrency, threshold)

# ---------------------------------------------------------------- Tab 历史

with tab_history:
    st.subheader("任务历史（SQLite 持久化）")
    jobs = store.list_jobs(limit=50)
    if not jobs:
        st.info("暂无任务记录。完成一次抽取后会出现在这里。")
    else:
        df = pd.DataFrame([
            {
                "任务": j["job_id"],
                "创建时间": j["created_at"],
                "模式": j["mode"],
                "表格": Path(j["table_path"]).name,
                "工作表": j["sheet"],
                "状态": j["status"],
                "成功": j["counts"].get("done", 0),
                "失败": j["counts"].get("failed", 0),
                "未开始": j["counts"].get("pending", 0),
            }
            for j in jobs
        ])
        st.dataframe(df, width="stretch", hide_index=True)
        st.caption("逐条状态（pending/running/done/failed）实时写入 data/jobs.db，失败项可在对应任务中重跑。")
