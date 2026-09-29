#!/usr/bin/env python3
"""对内容及来源发生变化的新闻重新评分；失败不写伪造分数。"""
import hashlib
import json
from common import completion, extract_text
from editorial import SYSTEM_PROMPT


def score_fingerprint(fields):
    payload = [extract_text(fields.get("内容概括")).strip(), extract_text(fields.get("聚合链接")).strip(), SYSTEM_PROMPT]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def needs_score(fields):
    score = fields.get("评分")
    valid = not isinstance(score, bool) and isinstance(score, (int, float)) and score in range(1, 6)
    return not valid or extract_text(fields.get("评分依据")) != score_fingerprint(fields)


def score_with_deepseek(client, content, aggregated_links=""):
    text = extract_text(content).strip()
    if not text:
        raise ValueError("空摘要不能作为正常新闻评分")
    links = set(link.strip() for link in extract_text(aggregated_links).split("、") if link.strip())
    prompt = f"内容概括：{text}\n聚合链接数量：{len(links)}"
    score = completion(client, prompt, system=SYSTEM_PROMPT, max_tokens=32)
    if score not in {"1", "2", "3", "4", "5"}:
        raise ValueError("模型评分不是 1~5 的整数")
    return int(score)


def run_score(api, record_ids, client):
    records = {r["record_id"]: r for r in api.records(api.config.destination_table)}
    if set(record_ids) - records.keys():
        raise ValueError("待评分记录缺失")
    for rid in record_ids:
        fields = records[rid].get("fields", {})
        if not needs_score(fields):
            continue
        score = score_with_deepseek(client, fields.get("内容概括"), fields.get("聚合链接"))
        api.update(api.config.destination_table, rid, {"评分": score, "评分依据": score_fingerprint(fields)})
        print(f"记录 {rid}：评分写入成功")


if __name__ == "__main__":
    raise SystemExit("请运行 python main.py，以执行带中断恢复的完整流程。")
