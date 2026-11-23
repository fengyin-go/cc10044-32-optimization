"""数据表同步回执台账：目录同步列版本一致性的唯一事实来源。

回执三要素：确切表名、目标目录、已验证列版本，缺一不可。台账规则：
- 精确匹配：只认完全一致的表名，同步范围里的相似表名不算已同步；
- 凭据唯一：同一凭据只能绑定首次登记的三要素，旧凭据不能抵新版欠账；
- 幂等登记：同一 (表名, 目标目录, 列版本) 重复送达只保留原凭据，不重复计数；
- 版本隔离：对账按列版本逐目录进行，旧版本回执不抵新版本欠账。

对账历史按变更编号存档，过去的对账可以随时查回。
"""
from __future__ import annotations

from typing import Any

from app.store import store

RECEIPT_TABLE = "_sync_receipt"      # 数据表同步回执（私有表，不进模块总览）
RECONCILE_TABLE = "_sync_reconcile"  # 对账历史（私有表，按变更编号查回）


def version_seq(version: str) -> int:
    """列版本号 → 序号，如 V2 → 2；无法解析时返回 -1 排到最后。"""
    text = str(version or "").strip().upper()
    if text.startswith("V") and text[1:].isdigit():
        return int(text[1:])
    return -1


def version_label(seq: int) -> str:
    return f"V{seq}"


def previous_version(version: str) -> str | None:
    """上一版列版本；V1 没有上一版，返回 None。"""
    seq = version_seq(version)
    if seq <= 1:
        return None
    return version_label(seq - 1)


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def register_receipt(
    *,
    receipt_id: str,
    table: str,
    target_catalog: str,
    column_version: str,
    received_at: str = "",
) -> dict[str, Any]:
    """登记一条数据表同步回执。

    返回 {"ok", "counted", "状态", "原凭据", "message"}：
    - 三要素不全 → ok=False，不入账；
    - 凭据已绑定其他三要素 → ok=False，旧凭据不能抵新版欠账；
    - 三要素已有已验证回执 → 视为重发，只保留原凭据，记一条“重复忽略”
      审计行，不重复计数；
    - 否则记为已验证回执，计入对账。
    """
    receipt_id = str(receipt_id or "").strip()
    table = str(table or "").strip()
    target_catalog = str(target_catalog or "").strip()
    column_version = str(column_version or "").strip().upper()

    missing = [
        name for name, value in (
            ("回执凭据", receipt_id), ("表名", table),
            ("目标目录", target_catalog), ("列版本", column_version))
        if not value
    ]
    if missing:
        return {"ok": False, "counted": False,
                "message": f"回执缺少要素：{'、'.join(missing)}，必须对应确切表名、目标目录和已验证列版本"}

    base = {"表名": table, "目标目录": target_catalog, "列版本": column_version}
    rows = store.rows(RECEIPT_TABLE)

    # 凭据唯一：同一凭据首次绑定哪条三要素，就永远只认那条。
    for row in rows:
        if row.get("receipt_id") == receipt_id:
            same = (row.get("表名") == table
                    and row.get("目标目录") == target_catalog
                    and row.get("列版本") == column_version)
            if not same:
                return {
                    **base, "ok": False, "counted": False, "状态": "凭据不符",
                    "原凭据": row.get("receipt_id"),
                    "message": (f"凭据 {receipt_id} 已绑定 {row.get('表名')}@"
                                f"{row.get('目标目录')} {row.get('列版本')}，"
                                f"不能抵 {table}@{target_catalog} {column_version} 的欠账"),
                }

    # 幂等：三要素已有已验证回执 → 重发只保留原凭据，不重复计数。
    original = next(
        (row for row in rows
         if row.get("状态") == "已验证"
         and row.get("表名") == table
         and row.get("目标目录") == target_catalog
         and row.get("列版本") == column_version),
        None,
    )
    if original is not None:
        rows.append({
            "id": _next_id(rows),
            "receipt_id": receipt_id,
            "表名": table,
            "目标目录": target_catalog,
            "列版本": column_version,
            "状态": "重复忽略",
            "原凭据": original.get("receipt_id"),
            "收到时间": received_at,
        })
        return {
            **base, "ok": True, "counted": False, "状态": "重复忽略",
            "原凭据": original.get("receipt_id"),
            "message": (f"{table}@{target_catalog} {column_version} 已有已验证回执，"
                        f"重发只保留原凭据 {original.get('receipt_id')}，不重复计数"),
        }

    rows.append({
        "id": _next_id(rows),
        "receipt_id": receipt_id,
        "表名": table,
        "目标目录": target_catalog,
        "列版本": column_version,
        "状态": "已验证",
        "原凭据": receipt_id,
        "收到时间": received_at,
    })
    return {
        **base, "ok": True, "counted": True, "状态": "已验证", "原凭据": receipt_id,
        "message": f"{table}@{target_catalog} {column_version} 回执已验证入账",
    }


def verified_receipts(
    *,
    table: str | None = None,
    target_catalog: str | None = None,
    column_version: str | None = None,
) -> list[dict[str, Any]]:
    """按确切表名/目标目录/列版本筛选已验证回执，不做任何模糊匹配。"""
    rows = [row for row in store.rows(RECEIPT_TABLE) if row.get("状态") == "已验证"]
    if table is not None:
        rows = [row for row in rows if row.get("表名") == table]
    if target_catalog is not None:
        rows = [row for row in rows if row.get("目标目录") == target_catalog]
    if column_version is not None:
        rows = [row for row in rows if row.get("列版本") == str(column_version).upper()]
    return rows


def is_confirmed(table: str, target_catalog: str, column_version: str) -> bool:
    """该目录是否已确认此表此列版本（存在已验证回执）。"""
    return bool(verified_receipts(
        table=table, target_catalog=target_catalog, column_version=column_version))


def receipt_credential(table: str, target_catalog: str, column_version: str) -> str | None:
    """该三要素最早入账的已验证回执凭据（原凭据）。"""
    rows = verified_receipts(
        table=table, target_catalog=target_catalog, column_version=column_version)
    return rows[0].get("receipt_id") if rows else None


def confirmed_versions(table: str, target_catalog: str) -> list[str]:
    """该目录对此表已确认的列版本列表，按版本序号升序。"""
    versions = {
        row.get("列版本")
        for row in verified_receipts(table=table, target_catalog=target_catalog)
    }
    return sorted(versions, key=version_seq)


def record_reconciliation(change_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """把一次对账结果存档，可按变更编号查回。"""
    rows = store.rows(RECONCILE_TABLE)
    record = {
        "id": _next_id(rows),
        "变更编号": int(change_id),
        "对账序号": sum(1 for row in rows if row.get("变更编号") == int(change_id)) + 1,
    }
    record.update(payload)
    rows.append(record)
    return record


def reconciliations_for(change_id: int) -> list[dict[str, Any]]:
    """按变更编号查回全部历史对账，按对账序号升序。"""
    return [row for row in store.rows(RECONCILE_TABLE)
            if row.get("变更编号") == int(change_id)]
