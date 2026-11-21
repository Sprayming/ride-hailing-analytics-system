#!/usr/bin/env python3
"""
网约车运营 NL2SQL 评测脚本

功能：
  1. 用可复现随机种子生成一个独立的评测数据库（不污染 data/ride_hailing.db）
  2. 逐条执行 evaluation_set.json 中的 gold SQL，校验其可执行且返回合理结果
  3. （可选）传入 --with-llm 时，调用真实 NL2SQL 管线生成 SQL，
     用 execution accuracy（执行结果集合相等）对比 gold SQL，量化准确率

用法：
  # 仅校验评测集 gold SQL 本身是否正确（无需 LLM key）
  python eval/nl2sql/run_eval.py --seed 42 --drivers 60 --orders 800 --coupons 400

  # 跑真实 NL2SQL 准确率（需要在 .env 中配置 LLM_API_KEY）
  python eval/nl2sql/run_eval.py --with-llm --seed 42 --drivers 60 --orders 800 --coupons 400
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# 将项目根加入 sys.path，便于导入 scripts / app 包
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import scripts.generate_data as gen  # noqa: E402


def build_eval_db(
    db_path: Path,
    seed: int | None,
    drivers: int,
    orders: int,
    coupons: int,
    reference_time: str,
) -> None:
    """用固定随机种子和参考时间生成独立评测库。

    只冻结数据生成器里的时钟，不改业务库生成脚本；gold SQL 使用同一参考时间，
    因此换日期、换机器重跑仍得到相同数据与相对时间窗口。
    """
    frozen = datetime.strptime(reference_time, "%Y-%m-%d %H:%M:%S")

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz is None else frozen.replace(tzinfo=tz)

    conn = sqlite3.connect(str(db_path))
    original_datetime = gen.datetime
    gen.datetime = FrozenDateTime
    try:
        drop = (
            "DROP TABLE IF EXISTS drivers; "
            "DROP TABLE IF EXISTS coupon_types; "
            "DROP TABLE IF EXISTS coupons; "
            "DROP TABLE IF EXISTS orders; "
            "DROP TABLE IF EXISTS redemptions;"
        )
        conn.executescript(drop)
        schema = (ROOT / "data" / "schema_sqlite.sql").read_text(encoding="utf-8")
        conn.executescript(schema)

        if seed is not None:
            gen.random.seed(seed)

        drivers_list = gen.generate_drivers(conn, drivers)
        gen.generate_coupon_types(conn)
        coupons_list = gen.generate_coupons(conn, coupons, drivers)
        orders_list = gen.generate_orders(conn, orders, drivers_list)
        gen.generate_redemptions(conn, coupons_list, orders_list)
    finally:
        gen.datetime = original_datetime
        conn.close()


def run_sql(db_path: Path, sql: str):
    """执行 SQL，返回 (rows, columns)。出错返回 ([], [])。"""
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows, cols
    except Exception as e:  # noqa: BLE001
        return [], [f"ERROR: {e}"]


def result_signature(rows):
    """将结果集转为可比较的签名（忽略行顺序与列名，仅比数据值）。"""
    return sorted(tuple(r.values()) for r in rows)


def validate_expect(case: dict, rows, cols) -> bool:
    """根据 expect 字段校验结果形态是否合理。"""
    expect = case.get("expect", "multi_row")
    if len(rows) == 0:
        return False
    if expect == "scalar":
        return len(rows) == 1 and len(cols) == 1
    return True  # multi_row: 至少一行即可


def dataset_fingerprint(path: Path) -> str:
    """评测集内容指纹，用于判断两次运行是否可比。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def normalize_rows(rows) -> list:
    """把结果行转成可比较的形态：浮点保留 3 位，忽略行序。"""
    normalized = []
    for row in rows:
        values = row.values() if isinstance(row, dict) else row
        normalized.append(tuple(
            round(value, 3) if isinstance(value, float) else value for value in values
        ))
    return sorted(normalized, key=repr)


def evaluate_semantic_layer(cases: list, db_path: Path) -> list:
    """逐条编译语义层计划，与 gold 结果比对。

    这一层不调用模型，衡量的是「目录能否表达这道题」以及「口径是否与 gold 一致」。
    与 gold 不一致时保留两边的值，用于区分实现缺陷与口径分歧。
    """
    from app.nlsql.semantic import compiler
    from app.nlsql.semantic import executor as semantic_executor
    from app.nlsql.semantic.types import Plan

    print("=" * 64)
    print("语义层覆盖评估（编译预期计划，不调模型）")
    records = []
    for case in cases:
        route = case.get("route", "freeform")
        record = {"case_id": case["id"], "route": route, "hit": False,
                  "expect": case.get("semantic_expect", "match"),
                  "note": case.get("semantic_note", "")}
        if route != "semantic" or not case.get("expected_plan"):
            records.append(record)
            continue

        gold_rows, _ = run_sql(db_path, case["gold_sql"])
        try:
            sql = compiler.compile_plan(Plan(**case["expected_plan"]))
            rows, columns = semantic_executor.execute(sql.text, sql.params, db_path=db_path)
        except Exception as exc:  # noqa: BLE001 —— 编译或执行失败都算未覆盖
            record["error"] = str(exc)
            records.append(record)
            print(f"  [ERR ] {case['id']}: {exc}")
            continue

        record["hit"] = normalize_rows(gold_rows) == normalize_rows(rows)
        record["semantic_rows"] = rows
        record["gold_rows"] = [[value for value in row.values()] for row in gold_rows]
        records.append(record)

        mark = "HIT " if record["hit"] else "GAP "
        print(f"  [{mark}] {case['id']}: {case['question']}")
        if not record["hit"]:
            print(f"         gold = {record['gold_rows'][:3]}")
            print(f"         sema = {rows[:3]}")
            if record["note"]:
                print(f"         说明 = {record['note']}")

    total = len(cases)
    semantic_total = sum(1 for item in records if item["route"] == "semantic")
    same_scale = [item for item in records if item["route"] == "semantic" and item["expect"] == "match"]
    gaps = [item for item in records if item["route"] == "semantic" and item["expect"] == "definition_gap"]
    hits = sum(1 for item in same_scale if item["hit"])
    print("-" * 64)
    print(f"语义层可表达: {semantic_total}/{total} = {semantic_total / total * 100:.1f}%")
    if same_scale:
        print(f"口径一致子集命中: {hits}/{len(same_scale)} = {hits / len(same_scale) * 100:.1f}%")
    if gaps:
        matched = sum(1 for item in gaps if item["hit"])
        print(f"口径/形态分歧子集: {len(gaps)} 条（其中 {matched} 条数值恰好相同）")
    unexpected = [item["case_id"] for item in same_scale if not item["hit"]]
    if unexpected:
        print(f"[WARN] 口径一致却未命中，需排查: {unexpected}")
    print(f"需自由 SQL: {total - semantic_total}/{total}")
    return records


def write_report(path: Path, payload: dict) -> None:
    """写出机器可读报告，供 evalhub 等下游消费。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="网约车 NL2SQL 评测")
    parser.add_argument("--eval-file", default=str(ROOT / "eval" / "nl2sql" / "evaluation_set.json"))
    parser.add_argument("--db", default=str(ROOT / "data" / "eval_nl2sql.db"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--drivers", type=int, default=60)
    parser.add_argument("--orders", type=int, default=800)
    parser.add_argument("--coupons", type=int, default=400)
    parser.add_argument("--with-llm", action="store_true", help="调用真实 NL2SQL 管线计算准确率（需 LLM_API_KEY）")
    parser.add_argument("--json-out", default=None, help="写出机器可读报告的路径（供 evalhub 消费）")
    args = parser.parse_args()

    db_path = Path(args.db)
    eval_path = Path(args.eval_file)
    if not eval_path.exists():
        print(f"[FATAL] 评测集不存在: {eval_path}")
        sys.exit(1)

    print("=" * 64)
    print("Step 1/2: 构建可复现评测库 ...")
    eval_payload = json.loads(eval_path.read_text(encoding="utf-8"))
    cases = eval_payload["cases"]
    evaluation_meta = eval_payload.get("meta", {})
    repro = evaluation_meta.get("repro", {})
    reference_time = repro["reference_time"]

    build_eval_db(
        db_path, args.seed, args.drivers, args.orders, args.coupons, reference_time
    )
    print(f"  评测库: {db_path}")
    print(f"  参考时间: {reference_time}")
    print(f"  评测用例: {len(cases)} 条")
    print("=" * 64)

    # ---- gold SQL 校验 ----
    print("Step 2/2: 校验 gold SQL 正确性")
    gold_pass = 0
    gold_records = []
    for case in cases:
        rows, cols = run_sql(db_path, case["gold_sql"])
        ok = validate_expect(case, rows, cols)
        gold_pass += 1 if ok else 0
        gold_records.append({
            "case_id": case["id"],
            "category": case.get("category", ""),
            "question": case.get("question", ""),
            "passed": bool(ok),
        })
        status = "OK " if ok else "FAIL"
        preview = ""
        if rows and cols:
            preview = f"  | cols={cols}  sample={rows[0]}"
        print(f"  [{status}] {case['id']} ({case['category']}): {case['question']}{preview}")

    gold_rate = gold_pass / len(cases) * 100
    print("-" * 64)
    print(f"Gold SQL 校验通过率: {gold_pass}/{len(cases)} = {gold_rate:.1f}%")

    # ---- 语义层覆盖评估（不调模型：直接编译预期计划） ----
    semantic_records = evaluate_semantic_layer(cases, db_path)
    semantic_cases = [item for item in semantic_records if item["route"] == "semantic"]
    same_scale = [item for item in semantic_cases if item.get("expect") == "match"]
    same_scale_hits = sum(1 for item in same_scale if item["hit"])
    semantic_gaps = [item for item in semantic_cases if item.get("expect") == "definition_gap"]

    # ---- 可选：真实 NL2SQL 准确率 ----
    if args.with_llm:
        try:
            from app.nlsql.sql_generator import generate_sql
            from app.nlsql.sql_executor import validate_sql
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 无法导入 NL2SQL 管线（缺少依赖或 LLM 配置）: {e}")
            sys.exit(2)

        print("=" * 64)
        print("真实 NL2SQL 准确率评估（execution accuracy）")
        match = 0
        llm_raw_records = []
        for case in cases:
            gold_rows, _ = run_sql(db_path, case["gold_sql"])
            gold_sig = result_signature(gold_rows)
            try:
                pred_sql, _ = generate_sql(case["question"])
                if not validate_sql(pred_sql):
                    pred_sql = ""
            except Exception as e:  # noqa: BLE001
                pred_sql = ""
                print(f"  [ERR ] {case['id']} 生成失败: {e}")

            pred_rows, _ = run_sql(db_path, pred_sql) if pred_sql else ([], [])
            pred_sig = result_signature(pred_rows)
            hit = gold_sig == pred_sig
            match += 1 if hit else 0
            llm_raw_records.append({
                "case_id": case["id"],
                "category": case.get("category", ""),
                "question": case.get("question", ""),
                "passed": hit,
                "pred_sql": pred_sql,
            })
            print(f"  [{'HIT ' if hit else 'MISS'}] {case['id']}: {case['question']}")
            if not hit:
                print(f"        gold  = {case['gold_sql']}")
                print(f"        pred  = {pred_sql}")

        acc = match / len(cases) * 100
        print("-" * 64)
        print(f"NL2SQL 执行准确率: {match}/{len(cases)} = {acc:.1f}%")
        llm_records = [
            {
                "case_id": item["case_id"],
                "category": item.get("category", ""),
                "question": item.get("question", ""),
                "passed": item.get("passed", False),
                "pred_sql": item.get("pred_sql", ""),
            }
            for item in llm_raw_records
        ]
        model_name = None
        try:
            from app.config import settings
            model_name = settings.llm_model
        except Exception:  # noqa: BLE001
            model_name = None
    else:
        print("（未启用 --with-llm，跳过真实 NL2SQL 评估；配置 LLM_API_KEY 后可加 --with-llm 量化准确率）")
        llm_records = None
        model_name = None

    if args.json_out:
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "with_llm" if args.with_llm else "gold_only",
            "model": model_name,
            "config": {
                "seed": args.seed,
                "drivers": args.drivers,
                "orders": args.orders,
                "coupons": args.coupons,
                "reference_time": reference_time,
            },
            "dataset": {
                "name": "ride_nl2sql",
                "version": (evaluation_meta.get("version") or "unknown"),
                "sha256": dataset_fingerprint(eval_path),
                "cases": len(cases),
                "seed": args.seed,
            },
            "metrics": {
                "gold_pass_rate": gold_pass / len(cases) if cases else None,
                "execution_accuracy": (match / len(cases)) if args.with_llm and cases else None,
                "semantic_coverage": (len(semantic_cases) / len(cases)) if cases else None,
                "semantic_accuracy": (same_scale_hits / len(same_scale)) if same_scale else None,
                "semantic_definition_gap": len(semantic_gaps),
                "semantic_gap_cases": [item["case_id"] for item in semantic_gaps],
            },
            "semantic_cases": semantic_records,
            "cases": llm_records if llm_records is not None else gold_records,
        }
        write_report(Path(args.json_out), payload)
        print(f"机器可读报告: {args.json_out}")

    print("=" * 64)
    # 退出码：gold 校验必须 100% 通过
    sys.exit(0 if gold_pass == len(cases) else 1)


if __name__ == "__main__":
    main()
