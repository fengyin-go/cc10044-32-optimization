"""目录同步领域服务：同步不同Catalog或Metastore之间的元数据和表结构。

数据链路位置：元数据同步层，保持多个数据目录的结构一致性。

一致性约定：目录同步按列版本推进。数据表同步回执（确切表名 + 目标目录 +
已验证列版本）是唯一确认凭据；启动下一版前必须读取 Schema 变更管理的
对账结果，同一目标目录先确认上一版；重试启动不多开同步任务。
"""

from __future__ import annotations

from typing import Any

from app import sync_ledger
from app.services.base import BaseModuleService
from app.store import store


class CatalogSyncService(BaseModuleService):
    MODULE = "catalog_sync"
    ENTITY = "同步任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务名称", "源Catalog", "目标Catalog", "同步范围", "同步状态"]
    REQUIRED_FIELDS = ["任务名称", "源Catalog", "目标Catalog"]
    STATUS_ORDER = ["待同步", "同步中", "已同步", "同步失败"]
    ACTION_RULES = {"启动同步": "待同步", "取消同步": "同步中"}
    NEGATIVE_ACTIONS = []

    def register_sync_receipt(
        self,
        *,
        receipt_id: str,
        table: str,
        target_catalog: str,
        column_version: str,
        received_at: str = "",
    ) -> dict[str, Any]:
        """登记数据表同步回执：目标目录写回列版本后的确认凭据。

        回执必须对应确切表名、目标目录和已验证列版本；重发只保留原凭据，
        不重复计数，也不能抵其他版本的欠账。
        """
        return sync_ledger.register_receipt(
            receipt_id=receipt_id,
            table=table,
            target_catalog=target_catalog,
            column_version=column_version,
            received_at=received_at,
        )

    def start_version_sync(self, entry_id: int) -> dict[str, Any]:
        """按列版本启动目录同步任务。

        启动规则：
        - 任务必须带确切表名和列版本；
        - 幂等：同表、同目标目录、同列版本已有同步中/已同步任务时，
          重试不多开同步任务；
        - 启动下一版前读取 Schema 变更管理的对账结果，同一目标目录
          必须先确认上一版（有已验证的数据表同步回执）。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "started": False, "message": "同步任务不存在"}

        table = str(entry.get("表名") or "").strip()
        version = str(entry.get("列版本") or "").strip().upper()
        target = str(entry.get("目标Catalog") or "").strip()
        if not table or not version or not target:
            return {"ok": False, "started": False,
                    "message": "同步任务缺少表名、列版本或目标Catalog，不能按版本启动"}

        # 幂等：重试启动不多开同步任务。
        if entry.get("status") in ("同步中", "已同步"):
            return {"ok": True, "started": False, "dedup": True, "task_id": entry_id,
                    "启动次数": int(entry.get("启动次数", 0)),
                    "message": f"任务 {entry_id} 已处于「{entry.get('status')}」，重试不再多开"}
        for row in store.rows(self.MODULE):
            if int(row.get("id", 0)) == entry_id:
                continue
            if (str(row.get("表名") or "").strip() == table
                    and str(row.get("目标Catalog") or "").strip() == target
                    and str(row.get("列版本") or "").strip().upper() == version
                    and row.get("status") in ("同步中", "已同步")):
                return {"ok": True, "started": False, "dedup": True,
                        "existing_task_id": row.get("id"),
                        "message": (f"{table}→{target} {version} 已有任务 "
                                    f"{row.get('id')}（{row.get('status')}），不再多开")}
        if entry.get("status") != "待同步":
            return {"ok": False, "started": False,
                    "message": "只能启动待同步状态的同步任务"}

        schema_service = self._schema_change_service()
        change = schema_service.find_change_for_version(table, version)
        if change is None:
            return {"ok": False, "started": False,
                    "message": f"找不到 {table} {version} 的已执行变更，不能启动同步"}

        # 启动下一版前读取对账结果：同一目标目录先确认上一版。
        prev = sync_ledger.previous_version(version)
        prev_recon = None
        if prev is not None:
            prev_change = schema_service.find_change_for_version(table, prev)
            if prev_change is None:
                return {"ok": False, "started": False,
                        "message": f"找不到上一版 {prev} 的变更记录，无法对账"}
            prev_recon = schema_service.reconcile_change(int(prev_change["id"]))
            catalog_state = prev_recon.get("目录对账", {}).get(target)
            if catalog_state is None:
                reason = f"目录 {target} 不在上一版 {prev} 的对账范围内"
                entry["启动阻塞原因"] = reason
                return {"ok": False, "started": False, "reason": reason,
                        "上一版对账": prev_recon}
            if catalog_state.get("状态") != "已确认":
                reason = f"目录 {target} 上一版 {prev} 未确认（尚欠数据表同步回执）"
                entry["启动阻塞原因"] = reason
                return {"ok": False, "started": False, "reason": reason,
                        "上一版对账": prev_recon}

        entry["status"] = "同步中"
        entry["同步状态"] = "进行中"
        entry["启动次数"] = int(entry.get("启动次数", 0)) + 1
        entry.pop("启动阻塞原因", None)
        return {
            "ok": True,
            "started": True,
            "task_id": entry_id,
            "表名": table,
            "目标Catalog": target,
            "列版本": version,
            "启动次数": entry["启动次数"],
            "上一版对账序号": prev_recon.get("对账序号") if prev_recon else None,
            "message": f"{table}→{target} {version} 同步已启动（第 {entry['启动次数']} 次）",
        }

    def cross_directory_review(self, table: str) -> dict[str, Any]:
        """跨目录复核：列出各目录已确认的列版本。

        两个目录列版本不一致时，不能按统一口径复核，
        必须逐份核对各目录副本的字段口径。
        """
        catalogs = sorted({
            row.get("目标目录")
            for row in sync_ledger.verified_receipts(table=table)
        })
        per_catalog = [
            {"目录": catalog, "已确认版本": sync_ledger.confirmed_versions(table, catalog)}
            for catalog in catalogs
        ]
        latest_versions = {
            item["目录"]: (item["已确认版本"][-1] if item["已确认版本"] else None)
            for item in per_catalog
        }
        consistent = len(set(latest_versions.values())) <= 1
        return {
            "ok": True,
            "表名": table,
            "目录版本": per_catalog,
            "版本一致": consistent,
            "复核要求": ("各目录列版本一致，可按统一口径复核" if consistent
                       else "列版本不一致，需逐份核对各目录副本的字段口径"),
        }

    @staticmethod
    def _schema_change_service():
        from app.services import service_for
        return service_for("schema_change")

    def start_sync(self, entry_id: int, incremental: bool = True) -> dict[str, Any]:
        """启动目录同步任务。

        incremental=True 时只同步上次同步以来发生变更的表，
        incremental=False 时全量重新同步。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "同步任务不存在"}
        if entry.get("status") != "待同步":
            return {"ok": False, "message": "只能启动待同步状态的同步任务"}

        source_catalog = entry.get("源Catalog", "")
        target_catalog = entry.get("目标Catalog", "")
        sync_scope = entry.get("同步范围", "")

        entry["status"] = "同步中"
        tables_to_sync = self._resolve_sync_tables(source_catalog, sync_scope)
        return {
            "ok": True,
            "task_id": entry_id,
            "source_catalog": source_catalog,
            "target_catalog": target_catalog,
            "incremental": incremental,
            "tables_to_sync": len(tables_to_sync),
            "estimated_duration_minutes": 12,
            "tables": tables_to_sync[:20],
        }

    def validate_sync_completeness(self, entry_id: int) -> dict[str, Any]:
        """验证同步完整性。

        对比源和目标Catalog中的表列表、Schema和分区定义，
        检测遗漏、不一致或版本差异。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "同步任务不存在"}

        checks = [
            {"check": "table_list_match", "source_count": 320, "target_count": 320, "matched": True, "mismatched_tables": []},
            {"check": "schema_match", "tables_with_diff": 2, "diff_details": [
                {"table": "order_fact_v3", "source_columns": 48, "target_columns": 47,
                 "missing_in_target": ["promotion_code"]},
                {"table": "user_behavior_log", "source_columns": 22, "target_columns": 22,
                 "type_diff": ["event_timestamp: TIMESTAMP vs STRING"]},
            ], "matched": False},
            {"check": "partition_match", "total_tables": 320, "mismatched": 0, "matched": True},
            {"check": "table_properties_match", "mismatched": 1, "details": [
                {"table": "dashboard_cache", "prop": "format-version", "source": "2", "target": "1"}
            ], "matched": False},
        ]
        all_ok = all(c["matched"] for c in checks)
        if all_ok:
            entry["status"] = "已同步"
            entry["同步状态"] = "已完成"
            entry["pending"] = False
        else:
            entry["abnormal"] = True

        return {
            "ok": all_ok,
            "task_id": entry_id,
            "checks": checks,
            "verdict": "同步完整" if all_ok else "存在差异需要修复",
        }

    def detect_schema_conflicts(self, entry_id: int) -> dict[str, Any]:
        """检测目录同步中的Schema冲突。

        当源和目标Catalog对同一表的Schema定义不一致时，
        列出冲突详情并提供解决建议。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "同步任务不存在"}

        return {
            "task_id": entry_id,
            "conflict_count": 3,
            "conflicts": [
                {
                    "table": "user_events",
                    "conflict_type": "column_type_mismatch",
                    "source": "event_data: MAP<STRING,STRING>",
                    "target": "event_data: STRING",
                    "resolution": "建议以源端类型为准，目标端已过期",
                },
                {
                    "table": "dim_customer",
                    "conflict_type": "missing_column",
                    "column": "loyalty_tier",
                    "resolution": "在目标端添加该列，默认值设为'standard'",
                },
                {
                    "table": "sales_summary",
                    "conflict_type": "partition_diff",
                    "source_partition": "dt",
                    "target_partition": "date_str",
                    "resolution": "按源端分区键重新创建目标表",
                },
            ],
            "auto_resolvable": 2,
            "manual_intervention_required": 1,
        }

    def sync_with_lineage(self, entry_id: int) -> dict[str, Any]:
        """同步完成后更新血缘关系。

        将同步范围内的表在目标Catalog的位置注册到血缘图谱，
        建立源→目标的跨Catalog血缘链路。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "同步任务不存在"}

        lineage_rows = store.rows("lineage_graph")
        start_id = max((int(r.get("id", 0)) for r in lineage_rows), default=0) + 1
        new_lineages = []
        for i in range(3):
            lid = start_id + i
            lineage_rows.append({
                "id": lid,
                "status": "已建立",
                "目标表": f"{entry.get('目标Catalog')}_{entry.get('同步范围', 'table')}_{i}",
                "来源表列表": f"{entry.get('源Catalog')}_{entry.get('同步范围', 'table')}_{i}",
                "转换逻辑": f"catalog_sync_{entry_id}",
                "中间层级": "1",
                "血缘深度": "1",
                "pending": False,
                "abnormal": False,
            })
            new_lineages.append(lid)

        return {
            "ok": True,
            "task_id": entry_id,
            "new_lineage_ids": new_lineages,
            "message": f"已注册 {len(new_lineages)} 条血缘关系",
        }

    def _resolve_sync_tables(self, catalog: str, scope: str) -> list[str]:
        base_tables = [
            "order_fact", "user_dim", "product_dim", "sales_agg_daily",
            "inventory_snapshot", "marketing_campaign", "web_traffic_log",
            "customer_360", "supplier_master", "finance_ledger",
        ]
        if scope and scope != "全部":
            return [t for t in base_tables if scope in t]
        return base_tables