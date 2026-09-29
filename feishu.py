"""飞书 API：校验 HTTP、业务返回值和逐条操作结果。"""
import time
import requests


class Feishu:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.token = None
        self.expires_at = 0

    def _authorize(self):
        if self.token and time.monotonic() < self.expires_at:
            return
        response = self.session.post(
            self.config.base_url + "/auth/v3/tenant_access_token/internal",
            json={"app_id": self.config.app_id, "app_secret": self.config.app_secret},
            timeout=30)
        data = self._checked(response)
        self.token = data["tenant_access_token"]
        self.expires_at = time.monotonic() + max(0, int(data.get("expire", 7200)) - 120)

    @staticmethod
    def _checked(response):
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            # 不输出完整响应，避免把正文或敏感参数写入公开日志。
            raise RuntimeError(f"飞书 API 业务失败，code={data.get('code')}")
        return data

    def request(self, method, table, suffix="", **kwargs):
        self._authorize()
        url = (f"{self.config.base_url}/bitable/v1/apps/{self.config.app_token}"
               f"/tables/{table}/{suffix}")
        response = self.session.request(
            method, url, headers={"Authorization": f"Bearer {self.token}"},
            timeout=30, **kwargs)
        # 不盲目重试创建请求：连接超时可能发生在服务器已经写入之后。
        return self._checked(response).get("data", {})

    def records(self, table):
        records, page_token, seen = [], None, set()
        while True:
            params = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            data = self.request("GET", table, "records", params=params)
            records.extend(data.get("items", []))
            if not data.get("has_more"):
                return records
            page_token = data.get("page_token")
            if not page_token or page_token in seen:
                raise RuntimeError("飞书分页令牌缺失或重复")
            seen.add(page_token)

    def update(self, table, record_id, fields):
        data = self.request("PUT", table, f"records/{record_id}", json={"fields": fields})
        if data.get("record", {}).get("record_id") != record_id:
            raise RuntimeError("飞书更新未返回预期记录")

    def create(self, table, fields):
        data = self.request("POST", table, "records", json={"fields": fields})
        record_id = data.get("record", {}).get("record_id")
        if not record_id:
            raise RuntimeError("飞书创建未返回记录 ID")
        return record_id

    def delete(self, table, record_ids):
        for offset in range(0, len(record_ids), 500):
            batch = record_ids[offset:offset + 500]
            data = self.request("POST", table, "records/batch_delete", json={"records": batch})
            deleted = {r.get("record_id") for r in data.get("records", [])
                       if r.get("deleted") is True}
            if deleted != set(batch):
                raise RuntimeError("飞书未确认全部记录删除成功，保留聚合恢复记录")

    def validate_schema(self):
        required = {
            self.config.source_table: {"内容概括": {1}, "公司": {1}, "分类": {1, 3},
                "来源": {1}, "链接": {1, 15}, "时间": {1, 5}, "是否已清洗": {1, 3}, "清洗计划": {1}},
            self.config.destination_table: {"内容概括": {1}, "公司": {1}, "分类": {1, 3},
                "来源": {1}, "链接": {1, 15}, "时间": {1, 5}, "清洗方式": {1, 3},
                "聚合链接": {1}, "评分": {2}, "评分依据": {1}, "清洗键": {1}, "待删除记录": {1}},
        }
        schemas = {}
        for table, expected in required.items():
            fields, token, seen = {}, None, set()
            while True:
                params = {"page_size": 100}
                if token:
                    params["page_token"] = token
                data = self.request("GET", table, "fields", params=params)
                fields.update({f["field_name"]: f["type"] for f in data.get("items", [])})
                if not data.get("has_more"):
                    break
                token = data.get("page_token")
                if not token or token in seen:
                    raise RuntimeError("字段分页令牌异常")
                seen.add(token)
            invalid = [name for name, types in expected.items() if fields.get(name) not in types]
            if invalid:
                raise ValueError("表格缺少字段或类型不符：" + "、".join(invalid))
            schemas[table] = fields
        for name in ("链接", "时间"):
            if schemas[self.config.source_table][name] != schemas[self.config.destination_table][name]:
                raise ValueError(f"源表和目标表的「{name}」字段类型必须一致")
