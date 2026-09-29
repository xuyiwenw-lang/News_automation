#!/usr/bin/env python3
"""校验语义分组，保存合并结果后删除重复行，支持中断恢复。"""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from common import cleaning_keys, completion, extract_text, parse_json
from editorial import GROUPING_PROMPT, MERGE_PROMPT


def _parse_date(value, timezone="Asia/Shanghai", today=None):
    text = extract_text(value).strip()
    if not text:
        return None
    today = today or datetime.now(ZoneInfo(timezone)).date()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(ZoneInfo(timezone)).date() if parsed.tzinfo else parsed.date()
    except ValueError:
        pass
    import re
    match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", text)
    if match:
        try:
            parsed = date(today.year, int(match[1]), int(match[2]))
            # 年初收到 12 月新闻时，按上一年解释，避免落到未来。
            if parsed > today + timedelta(days=7):
                parsed = date(today.year - 1, parsed.month, parsed.day)
            return parsed
        except ValueError:
            return None
    try:
        return datetime.fromtimestamp(float(text) / 1000, ZoneInfo(timezone)).date()
    except (ValueError, OverflowError, OSError):
        return None


def validate_grouping(result, records):
    if not isinstance(result, dict) or not isinstance(result.get("groups"), list):
        raise ValueError("聚类响应缺少 groups 数组")
    record_map = {r["record_id"]: r for r in records}
    used = set()
    for group in result["groups"]:
        if not isinstance(group, dict):
            raise ValueError("聚类分组必须是对象")
        primary, duplicates = group.get("primary_record_id"), group.get("duplicate_record_ids")
        if not isinstance(primary, str) or not isinstance(duplicates, list) or not duplicates:
            raise ValueError("聚类分组必须包含主记录及非空重复列表")
        ids = [primary] + duplicates
        if any(not isinstance(rid, str) for rid in ids):
            raise ValueError("聚类 ID 必须是字符串")
        if len(ids) != len(set(ids)) or used.intersection(ids) or not set(ids) <= record_map.keys():
            raise ValueError("聚类存在未知 ID、重复 ID 或重叠分组，取消所有写入")
        used.update(ids)
    empty = result.get("empty_record_ids", [])
    if not isinstance(empty, list) or any(
        not isinstance(rid, str) or rid not in record_map or
        extract_text(record_map[rid].get("fields", {}).get("内容概括")).strip() or rid in used
        for rid in empty
    ):
        raise ValueError("模型将有效新闻判为空行，取消所有写入")
    # 不按模型指令删除空行；空内容由人工修复，避免丢失来源追踪信息。
    return result["groups"]


def cluster_with_deepseek(records, client):
    nonempty = [r for r in records if extract_text(r.get("fields", {}).get("内容概括")).strip()]
    if len(nonempty) < 2:
        return []
    simplified = [{"record_id": r["record_id"], **{
        key: extract_text(r.get("fields", {}).get(key)) for key in ("公司", "内容概括", "时间")
    }} for r in nonempty]
    serialized = json.dumps(simplified, ensure_ascii=False)
    if len(serialized) > 60000:
        raise ValueError("聚合候选超过 60000 字符，请缩小回溯窗口或人工分批处理")
    # 同一窗口统一比较，避免单公司/多公司分桶漏掉重复事件。
    result = parse_json(completion(client, GROUPING_PROMPT.format(records_json=serialized)))
    return validate_grouping(result, nonempty)


def all_links(fields):
    links = [part.strip() for part in extract_text(fields.get("聚合链接")).split("、") if part.strip()]
    raw = fields.get("链接")
    link = (raw.get("link") or raw.get("url") or "") if isinstance(raw, dict) else extract_text(raw)
    if link:
        links.append(link)
    return list(dict.fromkeys(links))


def resume_pending_merges(api):
    """恢复已成功写入主记录、但尚未确认删除完成的操作。"""
    table = api.config.destination_table
    records = {r["record_id"]: r for r in api.records(table)}
    pending = []
    touched = set()
    for primary_id, record in records.items():
        raw = extract_text(record.get("fields", {}).get("待删除记录")).strip()
        if not raw:
            continue
        journal = json.loads(raw)
        if not isinstance(journal, dict) or journal.get("version") != 1 or not isinstance(journal.get("duplicates"), dict):
            raise ValueError("聚合恢复信息损坏")
        duplicates = journal["duplicates"]
        ids = {primary_id, *duplicates}
        if primary_id in duplicates or touched.intersection(ids):
            raise ValueError("聚合恢复信息存在重叠")
        primary_keys = cleaning_keys(record["fields"])
        for rid, snapshot in duplicates.items():
            if rid in records and records[rid].get("fields", {}) != snapshot:
                raise ValueError("待删除新闻在聚合后被修改，停止删除以保留新内容")
            if not isinstance(snapshot, dict) or not cleaning_keys(snapshot) <= primary_keys:
                raise ValueError("聚合主记录未包含重复行的清洗键")
        touched.update(ids)
        pending.append((primary_id, [rid for rid in duplicates if rid in records]))
    # 所有恢复信息通过校验后才开始删除。
    for primary_id, duplicates in pending:
        if duplicates:
            api.delete(table, duplicates)
        api.update(table, primary_id, {"待删除记录": ""})


def run_aggregate(api, record_ids, client):
    table = api.config.destination_table
    record_map = {r["record_id"]: r for r in api.records(table)}
    missing = set(record_ids) - record_map.keys()
    if missing:
        raise ValueError("聚合候选记录缺失，停止运行")
    records = [record_map[rid] for rid in record_ids]
    groups = cluster_with_deepseek(records, client)
    deleted = set()
    for group in groups:
        primary = group["primary_record_id"]
        duplicates = group["duplicate_record_ids"]
        fields_list = [record_map[rid]["fields"] for rid in [primary] + duplicates]
        summary = completion(client, MERGE_PROMPT.format(news_list="\n".join(
            "- " + extract_text(fields.get("内容概括")) for fields in fields_list)), max_tokens=1024)
        keys = set().union(*(cleaning_keys(fields) for fields in fields_list))
        links = list(dict.fromkeys(link for fields in fields_list for link in all_links(fields)))
        split = any("拆分" in extract_text(fields.get("清洗方式")) for fields in fields_list)
        journal = json.dumps({"version": 1, "duplicates": {
            rid: record_map[rid]["fields"] for rid in duplicates}}, ensure_ascii=False)
        if len(journal.encode("utf-8")) > 80000:
            raise ValueError("聚合恢复信息过大，停止该组操作")
        api.update(table, primary, {"内容概括": summary, "清洗方式": "拆分后聚合" if split else "聚合",
                   "聚合链接": "、".join(links), "清洗键": "\n".join(sorted(keys)), "待删除记录": journal})
        # 上一步必须成功；删除失败时主记录中的恢复信息保留到下次执行。
        api.delete(table, duplicates)
        api.update(table, primary, {"待删除记录": ""})
        deleted.update(duplicates)
    return [rid for rid in record_ids if rid not in deleted]


if __name__ == "__main__":
    raise SystemExit("请运行 python main.py，以执行带中断恢复的完整流程。")
