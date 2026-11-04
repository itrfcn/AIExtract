"""命令行入口：直接把表格和文本丢文件夹里跑，不需要 Streamlit。

典型用法：
    python cli.py                              # 自动扫描 data/ 样例跑一遍
    python cli.py my_table.xlsx texts/*.txt    # 指定一张表 + 多个文本（模式 A）
    python cli.py --pair t1.xlsx:a.txt t2.xlsx:b.txt   # 成对批量（模式 B）
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# 确保能 import core / pipeline
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import settings  # noqa: E402
from pipeline.extract import estimate_tokens, extract_record  # noqa: E402
from pipeline.export import export_rows  # noqa: E402
from pipeline.providers import get_provider  # noqa: E402
from pipeline.schema import map_headers  # noqa: E402
from pipeline.segment import split_records  # noqa: E402
from pipeline.textsource import is_supported_text_file, read_text_file  # noqa: E402
from pipeline.workbook import inspect_table  # noqa: E402


# ---------- 控制台小工具 ----------

def _print_header(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def _status_mark(cell: dict) -> str:
    """置信度状态 → 字符标记。"""
    st = cell.get("status")
    conf = cell.get("confidence", 0.0)
    if st == "ok":
        return "ok"
    if st == "uncertain":
        return f"uncertain({conf:.2f})"
    if st == "not_mentioned":
        return "—"
    return st or "?"


def _row_for_export(specs, cells: dict) -> dict:
    """把 extract_record 的输出按 specs 顺序组装成 export_rows 需要的行 dict。"""
    row: dict = {}
    for spec in specs:
        c = cells.get(spec.key, {})
        val = c.get("value")
        if isinstance(val, list):
            val = "、".join(str(v) for v in val)
        row[spec.key] = val
    return row


# ---------- 自动扫描 data/ ----------

def _scan_samples() -> tuple[Path, list[Path]] | None:
    """扫描 data/ 下样例文件，返回 (第一张表, 所有文本)。找不到返回 None。"""
    data_dir = Path(__file__).resolve().parent / "data"
    tables = sorted(
        p for p in data_dir.glob("*.xlsx")
        if not p.name.startswith("~$") and "filled" not in p.stem.lower()
    )
    texts = sorted(
        p for p in data_dir.iterdir()
        if is_supported_text_file(p.name) and p.suffix.lower() != ".xlsx"
    )
    if tables and texts:
        return tables[0], texts
    return None


# ---------- 主流程 ----------

def run_single_batch(table_path: Path, text_paths: list[Path], provider) -> Path | None:
    """模式 A：一张表 + 多份文本（或多段文本文件）→ 追加多行。返回输出文件路径。"""
    _print_header(f"表格：{table_path.name}")

    # 1. 读表头、探测 header_row
    info = inspect_table(table_path)
    print(f"  工作表：{info.sheet}    表头行：{info.header_row}    有效列：{len(info.headers)}")
    print(f"  表头：{[c.header for c in info.headers]}")

    # 2. 列名三级映射
    headers = [c.header for c in info.headers]
    samples = {i: c.sample_values for i, c in enumerate(info.headers)}
    specs = map_headers(headers, samples=samples, provider=provider)
    for s in specs:
        origin = {"dict": "词典", "fuzzy": "模糊", "llm": "LLM", "custom": "自定义"}.get(s.origin, str(s.origin))
        if not s.enabled:
            print(f"    [跳过] {s.header}  →  已禁用")
        elif s.origin != "dict":
            print(f"    [{origin}] {s.header}  →  {s.key}  (score={s.mapping_score})")

    # 3. 读 + 切分所有文本
    all_records = []
    for tp in text_paths:
        text, _warnings = read_text_file(tp)
        recs = split_records(text, mode="auto", source_name=tp.name)
        print(f"  文本 {tp.name} ({len(text)} 字) → {len(recs)} 条记录")
        all_records.extend(recs)

    if not all_records:
        print("  ! 没有可抽取的记录")
        return None

    # 4. token 预估
    texts_preview = [r.text for r in all_records]
    est = estimate_tokens(texts_preview)
    print(f"  token 预估：约 {est['total_tokens']} tokens，{est['calls']} 次调用")
    print(f"  {est['rough_cost_note']}")

    # 5. 并发抽取
    threshold = settings.confidence_threshold
    workers = max(1, min(16, settings.concurrency))
    rows: list[dict] = []
    failed_count = 0
    all_empty_count = 0
    t0 = datetime.now()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="extract") as ex:
        futures = {ex.submit(extract_record, provider, r.text, specs, threshold): r for r in all_records}
        for done, fut in enumerate(as_completed(futures), 1):
            rec = futures[fut]
            try:
                cells = fut.result()
            except Exception as exc:  # noqa: BLE001 - 单条失败不能拖垮整个批次
                name = rec.name_hint or f"记录{rec.record_id}"
                print(f"  [{done}/{len(all_records)}] {name:<12} FAILED: {exc}")
                failed_count += 1
                continue  # 跳过不写，导出文件不会有空行

            # 全空记录（LLM 所有字段都返回 None）→ 跳过
            row = _row_for_export(specs, cells)
            if all(v is None or v == "" for v in row.values()):
                name = rec.name_hint or f"记录{rec.record_id}"
                print(f"  [{done}/{len(all_records)}] {name:<12} SKIP (所有字段未提及)")
                all_empty_count += 1
                continue

            rows.append(row)

            # 控制台输出：姓名 + 状态
            name = rec.name_hint or row.get("name") or f"记录{rec.record_id}"
            ok_cnt = sum(1 for c in cells.values() if c.get("status") == "ok")
            unc_cnt = sum(1 for c in cells.values() if c.get("status") == "uncertain")
            miss_cnt = sum(1 for c in cells.values() if c.get("status") == "not_mentioned")
            print(f"  [{done}/{len(all_records)}] {name:<12} ok={ok_cnt}  uncertain={unc_cnt}  未提及={miss_cnt}")

            # 详情：只展示非 ok 字段（简洁）
            for spec in specs:
                c = cells.get(spec.key, {})
                if c.get("status") != "ok":
                    v = c.get("value") if c.get("value") is not None else ""
                    print(f"      · {spec.header} = {v!r}  [{_status_mark(c)}]")

    elapsed = (datetime.now() - t0).total_seconds()

    if failed_count or all_empty_count:
        print(f"\n  跳过：失败 {failed_count} 条、全空 {all_empty_count} 条 → 实际写入 {len(rows)} 行")

    # 6. 导出
    if not rows:
        print("  ! 无结果行")
        return None

    result = export_rows(table_path, info.sheet, info.header_row, specs, rows)
    print(f"\n  耗时：{elapsed:.1f}s    行数：{len(rows)}")
    print(f"  输出：{result.path}")
    print(f"  写入单元格={result.written_cells}  跳过={result.skipped_cells}  警告={result.warnings}")
    print(f"  原表未动，另存为新文件。")
    return result.path


def run_pair(table_text_pairs: list[tuple[Path, Path]], provider) -> None:
    """模式 B：多张表各自配对自己的文本，成对跑。"""
    _print_header(f"成对批量，共 {len(table_text_pairs)} 对")
    for table_path, text_path in table_text_pairs:
        try:
            run_single_batch(table_path, [text_path], provider)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 失败 {table_path.name} ← {text_path.name}: {exc}")


# ---------- CLI ----------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="命令行抽取工具：表格 + 文本 → 抽取 → 导出，无需 Streamlit。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python cli.py                               # 扫描 data/ 样例跑一遍
  python cli.py table.xlsx text1.txt text2.txt    # 模式 A：一张表 + 多份文本
  python cli.py --pair t1.xlsx:a.txt t2.xlsx:b.txt  # 模式 B：成对批量
""",
    )
    p.add_argument("positional", nargs="*", help="表和文本文件的混合列表（模式 A）")
    p.add_argument("--pair", action="append", default=[],
                   help="成对表:text，可多次。例：--pair a.xlsx:1.txt --pair b.xlsx:2.txt")
    p.add_argument("--provider", choices=["auto", "openai", "deepseek", "ollama"], default="auto",
                   help="强制指定 provider（默认 auto 读 .env）")
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    # 强制 provider（覆盖 .env 中的 LLM_PROVIDER，自动触发 settings 重建）
    if args.provider != "auto":
        import os
        from importlib import reload

        from pipeline.providers import _build_cached

        if hasattr(_build_cached, "cache_clear"):
            _build_cached.cache_clear()

        os.environ["LLM_PROVIDER"] = args.provider
        import core.config as cfg_mod
        reload(cfg_mod)
        provider = get_provider(cfg_mod.settings)
    else:
        provider = get_provider()

    _print_header("文本抽取 + 自动填表 · CLI")
    print(f"  provider : {settings.provider_label}")
    print(f"  data_dir : {settings.data_dir}")
    print(f"  output_dir: {settings.output_dir}")
    print(f"  concurrency={settings.concurrency}  threshold={settings.confidence_threshold}")

    # 模式 B：成对
    if args.pair:
        pairs: list[tuple[Path, Path]] = []
        for item in args.pair:
            if ":" not in item:
                print(f"  ! --pair 参数格式应为 table:text，收到：{item}")
                return 2
            t, txt = item.split(":", 1)
            tp, xp = Path(t), Path(txt)
            if not tp.exists():
                print(f"  ! 表文件不存在：{tp}")
                return 2
            if not xp.exists():
                print(f"  ! 文本文件不存在：{xp}")
                return 2
            pairs.append((tp, xp))
        run_pair(pairs, provider)
        return 0

    # 模式 A：混合列表 or 无参扫描
    if args.positional:
        files = [Path(f) for f in args.positional]
        missing = [f for f in files if not f.exists()]
        if missing:
            print(f"  ! 文件不存在：{missing}")
            return 2

        tables = [f for f in files if f.suffix.lower() in (".xlsx", ".xlsm", ".csv")]
        texts = [f for f in files if f not in tables]
        if len(tables) == 0:
            print("  ! 没找到表格文件（.xlsx / .csv）")
            return 2
        if len(tables) > 1:
            # 多张表但没 --pair，退化成"每张表单独跑，用全部文本"
            for t in tables:
                run_single_batch(t, texts, provider)
            return 0
        run_single_batch(tables[0], texts, provider)
        return 0

    # 无参：自动扫描 data/
    scan = _scan_samples()
    if not scan:
        print("  ! data/ 里没找到样例文件。先跑一下：python data/build_samples.py")
        return 1
    table, texts = scan
    print(f"  自动扫描 → 表={table.name}  文本={[t.name for t in texts]}")
    run_single_batch(table, texts, provider)
    return 0


if __name__ == "__main__":
    sys.exit(main())
