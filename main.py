#!/usr/bin/env python3
"""每日新闻清洗入口：恢复 → 拆分 → 聚合 → 评分 → 标记完成。"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from config import Config
from common import extract_text, make_client
from feishu import Feishu
from split_news import run_split
from aggregate_news import _parse_date, resume_pending_merges, run_aggregate
from score_news import needs_score, run_score


def run_pipeline(api, client):
    config = api.config
    api.validate_schema()
    resume_pending_merges(api)
    source_records = [r for r in api.records(config.source_table)
                      if extract_text(r.get("fields", {}).get("是否已清洗")).strip() != "是"]
    print(f"待处理源记录：{len(source_records)} 条")
    new_ids = run_split(api, source_records, client)
    today = datetime.now(ZoneInfo(config.timezone)).date()
    start = today - timedelta(days=config.lookback_days)
    candidates = set(new_ids)
    for record in api.records(config.destination_table):
        fields = record.get("fields", {})
        if not extract_text(fields.get("内容概括")).strip():
            print(f"警告：目标记录 {record['record_id']} 摘要为空，请人工修复")
            continue
        news_date = _parse_date(fields.get("时间"), config.timezone, today)
        # 无新源记录时仍补跑未完成评分；使用滚动窗口跨越周一边界。
        if needs_score(fields) or (news_date and start <= news_date <= today):
            candidates.add(record["record_id"])
    surviving_ids = run_aggregate(api, sorted(candidates), client)
    run_score(api, surviving_ids, client)
    for record in source_records:
        api.update(config.source_table, record["record_id"], {"是否已清洗": "是"})
    print(f"完成：源记录 {len(source_records)} 条，候选 {len(candidates)} 条，保留 {len(surviving_ids)} 条")


def main():
    config = Config.from_env()
    run_pipeline(Feishu(config), make_client(config))


if __name__ == "__main__":
    main()
