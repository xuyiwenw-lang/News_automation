"""字段转换及模型响应校验。"""
import json
import re


def extract_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(extract_text(item) for item in value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("link") or value.get("url") or "")
    return str(value)


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def completion(client, prompt, *, system=None, max_tokens=8192):
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    result = client.chat.completions.create(
        model="deepseek-chat", messages=messages, temperature=0, max_tokens=max_tokens)
    choice = result.choices[0]
    if choice.finish_reason != "stop" or not choice.message.content:
        raise ValueError("模型响应不完整，保留待处理状态")
    return choice.message.content.strip()


def cleaning_keys(fields):
    return set(extract_text(fields.get("清洗键")).split())


def make_client(config):
    from openai import OpenAI
    return OpenAI(api_key=config.deepseek_key, base_url="https://api.deepseek.com",
                  timeout=90, max_retries=2)
