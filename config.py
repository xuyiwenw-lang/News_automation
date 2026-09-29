"""运行配置；不在仓库中保存凭据。"""
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    app_token: str
    source_table: str
    destination_table: str
    deepseek_key: str
    base_url: str = "https://open.feishu.cn/open-apis"
    timezone: str = "Asia/Shanghai"
    lookback_days: int = 7

    @classmethod
    def from_env(cls):
        names = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_APP_TOKEN",
                 "FEISHU_SOURCE_TABLE_ID", "FEISHU_DEST_TABLE_ID", "DEEPSEEK_API_KEY")
        missing = [name for name in names if not os.getenv(name, "").strip()]
        if missing:
            raise ValueError("缺少环境变量：" + ", ".join(missing))
        config = cls(*(os.environ[name].strip() for name in names),
                     base_url=os.getenv("FEISHU_BASE_URL", cls.base_url).rstrip("/"),
                     timezone=os.getenv("NEWS_TIMEZONE", cls.timezone),
                     lookback_days=int(os.getenv("NEWS_LOOKBACK_DAYS", "7")))
        ZoneInfo(config.timezone)
        if config.lookback_days < 1:
            raise ValueError("NEWS_LOOKBACK_DAYS 必须大于 0")
        if config.source_table == config.destination_table:
            raise ValueError("源表与目标表必须不同")
        if not config.base_url.startswith("https://"):
            raise ValueError("FEISHU_BASE_URL 必须使用 HTTPS")
        return config
