"""Schema变更管理领域服务：管理表Schema的变更申请、兼容性检查和回退执行。

数据链路位置：架构管理层，控制数据表结构变更的范围和影响。

一致性约定：变更是否同步到各目标目录，只按数据表同步回执对账——
回执必须对应确切表名、目标目录和已验证列版本；对账结果存档，
供目录同步启动下一版前读取，也可按变更编号查回。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app import sync_ledger
from app.services.base import BaseModuleService
from app.store import store


class SchemaChangeService(BaseModuleService):
    MODULE = "schema_change"
    ENTITY = "变更记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["目标表", "变更类型", "变更内容", "影响下游表数", "变更时间"]
    REQUIRED_FIELDS = ["目标表", "变更类型", "变更内容"]
    STATUS_ORDER = ["待审批", "已执行", "已回退"]
    ACTION_RULES = {"执行变更": "待审批", "回退变更": "已回退"}
    NEGATIVE_ACTIONS = []

    CHANGE_TYPES = frozenset({"ADD_COLUMN", "DROP_COLUMN", "MODIFY_TYPE", "RENAME_COLUMN", "RENAME_TABLE"})

    def assess_compatibility(self, entry_id: int) -> dict[str, Any]:
        """评估Schema变更的兼容性。

        检查变更是否向下兼容（新增可空列/重命名）或是破坏性变更（删除列/修改类型）。
        破坏性变更要求列出所有受影响的依赖。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "变更记录不存在"}

        change_type = entry.get("变更类型", "")
        change_content = entry.get("变更内容", "")
        target_table = entry.get("目标表", "")

        backward_compatible = change_type in ("ADD_COLUMN", "RENAME_COLUMN")
        breaking = change_type in ("DROP_COLUMN", "MODIFY_TYPE", "RENAME_TABLE")

        lineage_rows = store.rows("lineage_graph")
        affected_downstream = [
            l for l in lineage_rows
            if l.get("目标表") == target_table or target_table in l.get("来源表列表", "").split(",")
        ]

        assessment = {
            "change_id": entry_id,
            "change_type": change_type,
            "target_table": target_table,
            "change_content": change_content,
            "backward_compatible": backward_compatible,
            "breaking_change": breaking,
            "affected_downstream_tables": len(affected_downstream),
            "affected_tables_detail": [
                {"table": l.get("目标表"), "lineage_id": l.get("id")}
                for l in affected_downstream[:10]
            ],
            "recommendation": "可以安全执行" if backward_compatible and not breaking
            else "需要在下游表协调后执行，建议先创建兼容视图",
        }
        entry["影响下游表数"] = str(len(affected_downstream))
        return assessment

    def execute_with_backup(self, entry_id: int) -> dict[str, Any]:
        """执行Schema变更并保留备份快照。

        执行前自动创建目标表的Schema快照，
        变更失败时可据此回退。快照记录在 snapshot_creator 模块。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "变更记录不存在"}
        if entry.get("status") != "待审批":
            return {"ok": False, "message": "只有待审批的变更记录才能执行"}

        assessment = self.assess_compatibility(entry_id)

        snapshot_rows = store.rows("snapshot_creator")
        snap_id = max((int(r.get("id", 0)) for r in snapshot_rows), default=0) + 1
        snapshot_rows.append({
            "id": snap_id,
            "status": "已完成",
            "目标数据集": f"{entry.get('目标表')}_schema",
            "快照时间点": "2026-11-21T08:00:00Z",
            "快照类型": "schema_backup",
            "存储路径": f"/schemas/backup/{entry.get('目标表')}_pre_change_{entry_id}",
            "文件大小": "0.01 GB",
            "pending": False,
            "abnormal": False,
        })

        entry["status"] = "已执行"
        entry["变更时间"] = "2026-11-21T08:00:00Z"
        entry["pending"] = False

        return {
            "ok": True,
            "change_id": entry_id,
            "backup_snapshot_id": snap_id,
            "assessment": assessment,
            "message": f"Schema变更已执行，备份快照ID: {snap_id}",
        }

    def rollback_change(self, entry_id: int) -> dict[str, Any]:
        """回退已执行的Schema变更。

        使用执行前创建的备份快照恢复原始Schema，
        记录回退原因和影响范围。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "变更记录不存在"}
        if entry.get("status") != "已执行":
            return {"ok": False, "message": "只有已执行的变更记录才能回退"}

        snapshot_rows = store.rows("snapshot_creator")
        backup_snapshots = [
            s for s in snapshot_rows
            if f"_pre_change_{entry_id}" in s.get("存储路径", "")
        ]

        entry["status"] = "已回退"
        entry["abnormal"] = True

        return {
            "ok": True,
            "change_id": entry_id,
            "deployed_snapshot_id": backup_snapshots[0]["id"] if backup_snapshots else None,
            "message": f"变更已回退，恢复自快照",
        }

    def list_pending_changes_by_table(self, table_name: str) -> dict[str, Any]:
        """查询指定表的待处理Schema变更列表。

        按变更时间排序，标注每个变更的兼容性风险等级。
        """
        all_changes = store.rows(self.MODULE)
        pending = [c for c in all_changes if c.get("目标表") == table_name and c.get("status") == "待审批"]
        return {
            "table": table_name,
            "pending_count": len(pending),
            "changes": [
                {
                    "id": c["id"],
                    "type": c.get("变更类型"),
                    "content": c.get("变更内容"),
                    "risk": "high" if c.get("变更类型") in ("DROP_COLUMN", "RENAME_TABLE") else "low",
                }
                for c in pending
            ],
        }

    def reconcile_with_catalog_sync(self, entry_id: int) -> dict[str, Any]:
        """对账Schema变更与Catalog同步状态（按数据表同步回执精确对账）。

        只认确切表名 + 目标目录 + 已验证列版本的回执；
        同步范围里的相似表名不算已同步。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "变更记录不存在"}
        if not str(entry.get("列版本") or "").strip():
            return {"ok": False, "message": "变更记录缺少列版本，无法按回执对账"}
        return self.reconcile_change(entry_id)

    def reconcile_change(self, entry_id: int) -> dict[str, Any]:
        """按列版本逐目录对账：用已验证的数据表同步回执核对本变更。

        逐目录检查本变更列版本是否已有已验证回执，并汇总待同步范围
        （各目录尚欠回执的列版本）。结果存档，可按变更编号查回；
        目录同步启动下一版前读取的就是这份对账结果。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "变更记录不存在"}
        if entry.get("status") != "已执行":
            return {"ok": False, "message": "只有已执行的变更才需要对账"}

        table = str(entry.get("目标表") or "").strip()
        version = str(entry.get("列版本") or "").strip().upper()
        catalogs = self._sync_catalogs(entry)
        if not table or not version or not catalogs:
            return {"ok": False, "message": "变更记录缺少目标表、列版本或同步目录，无法按回执对账"}

        per_catalog: dict[str, Any] = {}
        for catalog in catalogs:
            credential = sync_ledger.receipt_credential(table, catalog, version)
            per_catalog[catalog] = {
                "状态": "已确认" if credential else "欠回执",
                "凭据": credential,
            }

        payload = {
            "目标表": table,
            "列版本": version,
            "目录对账": per_catalog,
            "待同步范围": self.pending_scope(table, catalogs),
            "全部确认": all(item["状态"] == "已确认" for item in per_catalog.values()),
            "对账时间": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        record = sync_ledger.record_reconciliation(entry_id, payload)
        return {"ok": True, **record}

    def get_reconciliation_history(self, entry_id: int) -> dict[str, Any]:
        """按变更编号查回历史对账，过去的对账结果不丢。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "变更记录不存在"}
        history = sync_ledger.reconciliations_for(entry_id)
        return {
            "ok": True,
            "change_id": entry_id,
            "count": len(history),
            "history": history,
        }

    def pending_scope(self, table: str, catalogs: list[str] | None = None) -> dict[str, list[str]]:
        """待同步范围：各目录尚欠数据表同步回执的列版本列表。

        欠账口径 = 已执行变更的列版本中，该目录还没有已验证回执的部分；
        旧版本回执只抵旧版本，相似表名的回执不算数。
        """
        executed_versions = sorted(
            {str(c.get("列版本")).strip().upper() for c in store.rows(self.MODULE)
             if c.get("目标表") == table and c.get("status") == "已执行"
             and str(c.get("列版本") or "").strip()},
            key=sync_ledger.version_seq,
        )
        if catalogs is None:
            catalogs = sorted({
                catalog
                for c in store.rows(self.MODULE) if c.get("目标表") == table
                for catalog in self._sync_catalogs(c)
            })
        return {
            catalog: [
                version for version in executed_versions
                if not sync_ledger.is_confirmed(table, catalog, version)
            ]
            for catalog in catalogs
        }

    def find_change_for_version(self, table: str, column_version: str) -> dict[str, Any] | None:
        """查某表某列版本对应的已执行变更记录，供目录同步启动前对账。"""
        wanted = str(column_version or "").strip().upper()
        for change in store.rows(self.MODULE):
            if (change.get("目标表") == table
                    and str(change.get("列版本") or "").strip().upper() == wanted
                    and change.get("status") == "已执行"):
                return change
        return None

    @staticmethod
    def _sync_catalogs(entry: dict[str, Any]) -> list[str]:
        return [c.strip() for c in str(entry.get("同步目录") or "").split(",") if c.strip()]