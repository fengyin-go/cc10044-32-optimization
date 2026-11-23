"""订单表跨目录同步一致性场景演练。

场景：ods_orders 同步到 catalog_a / catalog_b，两边都已确认 V1；
V2 已执行，A 的 V2 回执先到、B 的迟到，A 的 V1 回执又被重发；
V3 已执行且同步任务已排队（A 的独立报表在等 V3 新字段）。

演练顺序：
  1. 回执到达（含相似表名回执、旧凭据抵新版、V1 重发）；
  2. 对账 V2、回查 V1；
  3. 尝试启动两个目录的 V3 同步，记录开跑/待同步及原因；
  4. B 的 V2 回执到达后再次对账和启动；
  5. 核对 B 的 V2 欠账、V3 启动次数、V1 原凭据。

运行：cd backend && PYTHONPATH=. .venv/bin/python scripts/scenario_order_sync.py
"""
from __future__ import annotations

import json
import sys

from app import sync_ledger
from app.services import service_for
from app.store import store

catalog_sync = service_for("catalog_sync")
schema_change = service_for("schema_change")

CHANGE_V1, CHANGE_V2, CHANGE_V3 = 4, 5, 6
TASK_A_V3, TASK_B_V3 = 4, 5

FAILURES: list[str] = []


def show(title: str, payload) -> None:
    print(f"\n── {title} " + "─" * max(2, 60 - len(title) * 2))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "✓" if condition else "✗"
    print(f"  {mark} {name}" + (f"：{detail}" if detail else ""))
    if not condition:
        FAILURES.append(f"{name} {detail}")


print("=" * 64)
print("订单表跨目录同步一致性场景演练")
print("=" * 64)

# ── 步骤 0：初始状态 ─────────────────────────────────────────────
show("步骤0 初始状态：V1 两目录已确认，V2 已执行，V3 任务已排队", {
    "A 已确认版本": sync_ledger.confirmed_versions("ods_orders", "catalog_a"),
    "B 已确认版本": sync_ledger.confirmed_versions("ods_orders", "catalog_b"),
    "V3 任务": [
        {"id": t["id"], "目标Catalog": t["目标Catalog"], "列版本": t["列版本"],
         "status": t["status"]}
        for t in store.rows("catalog_sync") if t.get("列版本") == "V3"
    ],
})
check("初始 A/B 均只确认 V1",
      sync_ledger.confirmed_versions("ods_orders", "catalog_a") == ["V1"]
      and sync_ledger.confirmed_versions("ods_orders", "catalog_b") == ["V1"])

# ── 步骤 1：回执到达 ─────────────────────────────────────────────
r1 = catalog_sync.register_sync_receipt(
    receipt_id="rcpt-orders-a-v2", table="ods_orders",
    target_catalog="catalog_a", column_version="V2",
    received_at="2026-11-23T09:10:00Z")
r2 = catalog_sync.register_sync_receipt(
    receipt_id="rcpt-orders-b-v1", table="ods_orders",
    target_catalog="catalog_b", column_version="V2",
    received_at="2026-11-23T09:11:00Z")
r3 = catalog_sync.register_sync_receipt(
    receipt_id="rcpt-ext-b-v2", table="ods_orders_ext",
    target_catalog="catalog_b", column_version="V2",
    received_at="2026-11-23T09:12:00Z")
r4 = catalog_sync.register_sync_receipt(
    receipt_id="rcpt-orders-a-v1", table="ods_orders",
    target_catalog="catalog_a", column_version="V1",
    received_at="2026-11-23T09:13:00Z")
show("步骤1 回执到达", {
    "A 的 V2 回执（先到）": r1,
    "B 的 V1 旧凭据冒充 V2": r2,
    "相似表名 ods_orders_ext 的 V2 回执": r3,
    "A 的 V1 回执重发": r4,
})
check("A 的 V2 回执入账", r1["counted"] and r1["状态"] == "已验证")
check("旧凭据不能抵新版欠账", not r2["ok"] and r2["状态"] == "凭据不符", r2["message"])
check("相似表名回执只记在自己头上", r3["counted"] and r3["表名"] == "ods_orders_ext")
check("V1 重发只保留原凭据",
      not r4["counted"] and r4["状态"] == "重复忽略" and r4["原凭据"] == "rcpt-orders-a-v1")

# ── 步骤 2：对账 V2 ─────────────────────────────────────────────
recon_v2 = schema_change.reconcile_change(CHANGE_V2)
show("步骤2 对账 V2（变更 #5）", recon_v2)
check("A 的 V2 已确认", recon_v2["目录对账"]["catalog_a"]["状态"] == "已确认")
check("B 的 V2 欠回执（相似表名不算已同步）",
      recon_v2["目录对账"]["catalog_b"]["状态"] == "欠回执")
check("待同步范围：A 欠 V3", recon_v2["待同步范围"]["catalog_a"] == ["V3"])
check("待同步范围：B 欠 V2、V3", recon_v2["待同步范围"]["catalog_b"] == ["V2", "V3"])

# ── 步骤 3：回查 V1 ─────────────────────────────────────────────
recon_v1 = schema_change.reconcile_change(CHANGE_V1)
history_v1 = schema_change.get_reconciliation_history(CHANGE_V1)
show("步骤3 回查 V1（变更 #4）", {
    "V1 最新对账": recon_v1["目录对账"],
    "V1 对账历史条数": history_v1["count"],
})
check("V1 两目录均已确认",
      recon_v1["目录对账"]["catalog_a"]["状态"] == "已确认"
      and recon_v1["目录对账"]["catalog_b"]["状态"] == "已确认")
check("V1 对账可按变更编号查回", history_v1["count"] >= 1)
check("A 的 V1 原凭据未被重发顶替",
      recon_v1["目录对账"]["catalog_a"]["凭据"] == "rcpt-orders-a-v1")

# ── 步骤 4：第一轮启动 V3 ────────────────────────────────────────
start_a1 = catalog_sync.start_version_sync(TASK_A_V3)
start_b1 = catalog_sync.start_version_sync(TASK_B_V3)
retry_a = catalog_sync.start_version_sync(TASK_A_V3)
show("步骤4 第一轮启动 V3", {
    "A 的 V3 启动": start_a1,
    "B 的 V3 启动": start_b1,
    "A 的 V3 重试": retry_a,
})
started_round1 = [f"A(V3,任务{TASK_A_V3})"] if start_a1.get("started") else []
pending_round1 = [] if start_b1.get("started") else [
    {"任务": f"B(V3,任务{TASK_B_V3})", "原因": start_b1.get("reason")}]
print("\n  本轮实际开跑:", started_round1 or "无")
print("  本轮仍待同步:", json.dumps(pending_round1, ensure_ascii=False))
check("A 的 V3 实际开跑", start_a1.get("started") and start_a1["启动次数"] == 1)
check("B 的 V3 仍待同步：上一版 V2 未确认",
      not start_b1["started"] and "V2" in start_b1["reason"], start_b1["reason"])
check("A 重试不多开任务", retry_a.get("dedup") and retry_a["启动次数"] == 1)

# ── 步骤 5：跨目录复核 ───────────────────────────────────────────
review = catalog_sync.cross_directory_review("ods_orders")
show("步骤5 跨目录复核（A 的独立报表在等 V3 新字段）", review)
check("两目录列版本不一致，需逐份核对字段口径",
      not review["版本一致"] and "逐份核对" in review["复核要求"])

# ── 步骤 6：B 的 V2 回执到达 ─────────────────────────────────────
r5 = catalog_sync.register_sync_receipt(
    receipt_id="rcpt-orders-b-v2", table="ods_orders",
    target_catalog="catalog_b", column_version="V2",
    received_at="2026-11-23T11:30:00Z")
show("步骤6 B 的 V2 回执（迟到）到达", r5)
check("B 的 V2 回执入账", r5["counted"] and r5["状态"] == "已验证")

# ── 步骤 7：再次对账 V2 ──────────────────────────────────────────
recon_v2b = schema_change.reconcile_change(CHANGE_V2)
history_v2 = schema_change.get_reconciliation_history(CHANGE_V2)
show("步骤7 再次对账 V2", {
    "目录对账": recon_v2b["目录对账"],
    "待同步范围": recon_v2b["待同步范围"],
    "V2 对账历史条数": history_v2["count"],
})
check("B 的 V2 欠账已清", "V2" not in recon_v2b["待同步范围"]["catalog_b"],
      f"B 待同步范围={recon_v2b['待同步范围']['catalog_b']}")
check("B 的 V2 凭据是迟到回执", recon_v2b["目录对账"]["catalog_b"]["凭据"] == "rcpt-orders-b-v2")
check("V2 历次对账均可查回", history_v2["count"] >= 2)

# ── 步骤 8：再次启动 V3 ──────────────────────────────────────────
start_b2 = catalog_sync.start_version_sync(TASK_B_V3)
retry_a2 = catalog_sync.start_version_sync(TASK_A_V3)
retry_b2 = catalog_sync.start_version_sync(TASK_B_V3)
task_a = store.find("catalog_sync", TASK_A_V3)
task_b = store.find("catalog_sync", TASK_B_V3)
v3_running = [t for t in store.rows("catalog_sync")
              if t.get("表名") == "ods_orders" and t.get("列版本") == "V3"
              and t.get("status") == "同步中"]
show("步骤8 再次启动 V3", {
    "B 的 V3 启动": start_b2,
    "A 重试": retry_a2.get("message"),
    "B 重试": retry_b2.get("message"),
    "V3 启动次数": {"A": task_a["启动次数"], "B": task_b["启动次数"]},
})
check("B 的 V3 这次开跑", start_b2.get("started") and start_b2["启动次数"] == 1)
check("V3 启动次数：A=1、B=1，重试未多开",
      task_a["启动次数"] == 1 and task_b["启动次数"] == 1 and len(v3_running) == 2)

# ── 步骤 9：核对 V1 原凭据 ───────────────────────────────────────
v1_a = sync_ledger.verified_receipts(table="ods_orders", target_catalog="catalog_a", column_version="V1")
v1_b = sync_ledger.verified_receipts(table="ods_orders", target_catalog="catalog_b", column_version="V1")
resend_rows = [r for r in store.rows(sync_ledger.RECEIPT_TABLE) if r.get("状态") == "重复忽略"]
show("步骤9 核对 V1 原凭据", {
    "A 的 V1 已验证回执": v1_a,
    "B 的 V1 已验证回执": v1_b,
    "重发审计行": resend_rows,
})
check("V1 各目录只计 1 条已验证回执", len(v1_a) == 1 and len(v1_b) == 1)
check("V1 原凭据保留",
      v1_a[0]["receipt_id"] == "rcpt-orders-a-v1" and v1_b[0]["receipt_id"] == "rcpt-orders-b-v1")
check("重发行只留审计、指向原凭据",
      len(resend_rows) == 1 and resend_rows[0]["原凭据"] == "rcpt-orders-a-v1")

# ── 最终报告 ─────────────────────────────────────────────────────
print("\n" + "=" * 64)
print("最终报告")
print("=" * 64)
print("实际开跑的 V3 任务:")
print(f"  - 任务{TASK_A_V3} ods_orders→catalog_a（第1轮开跑，启动 {task_a['启动次数']} 次）")
print(f"  - 任务{TASK_B_V3} ods_orders→catalog_b（第2轮开跑，启动 {task_b['启动次数']} 次）")
print("曾待同步及原因:")
print(f"  - 任务{TASK_B_V3} 第1轮被拦：{start_b1['reason']}")
print(f"当前待同步范围: {json.dumps(schema_change.pending_scope('ods_orders'), ensure_ascii=False)}")

if FAILURES:
    print(f"\n✗ {len(FAILURES)} 项核对未通过：")
    for item in FAILURES:
        print(f"  - {item}")
    sys.exit(1)
print("\n✓ 全部核对通过")
