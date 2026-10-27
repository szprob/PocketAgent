"""查各家接口账户的余额 / 用量（设置页「用量」里的「账户余额」）。

按接口地址认：
- api.deepseek.com：GET /user/balance
- openrouter.ai：GET /api/v1/key（已用额度、上限）
- 其他 OpenAI 兼容网关（如 new-api）：试 OpenAI 旧版账单接口 /dashboard/billing/subscription + usage
其他的（比如智谱）没有公开的余额接口，显示「不支持」。
"""

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from pocketagent.config import ModelProfile

REQUEST_TIMEOUT_SECONDS = 15


@dataclass
class BalanceReport:
    provider: str
    models: list[str] = field(default_factory=list)
    ok: bool = False
    lines: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _host(profile: ModelProfile) -> str:
    return urlparse(profile.base_url or "").hostname or ""


def provider_label(profile: ModelProfile) -> str:
    host = _host(profile)
    if "deepseek.com" in host:
        return "DeepSeek 官方"
    if "openrouter.ai" in host:
        return "OpenRouter"
    if "bigmodel.cn" in host:
        return "智谱 BigModel"
    return host or "未填接口地址"


async def _deepseek_balance(client: httpx.AsyncClient, profile: ModelProfile) -> list[str]:
    response = await client.get("https://api.deepseek.com/user/balance", headers=_auth(profile))
    response.raise_for_status()
    data = response.json()
    lines = []
    for info in data.get("balance_infos", []):
        lines.append(
            f"余额 {info.get('total_balance')} {info.get('currency')}"
            f"（赠送 {info.get('granted_balance')}，充值 {info.get('topped_up_balance')}）"
        )
    if not data.get("is_available", True):
        lines.append("账户余额不足，当前不可用")
    return lines or ["没有余额信息"]


async def _openrouter_balance(client: httpx.AsyncClient, profile: ModelProfile) -> list[str]:
    response = await client.get("https://openrouter.ai/api/v1/key", headers=_auth(profile))
    response.raise_for_status()
    data = response.json().get("data", {})
    lines = [f"已用 ${float(data.get('usage') or 0):.4f}"]
    limit = data.get("limit")
    if limit is not None:
        lines.append(f"额度上限 ${float(limit):.2f}，剩余 ${float(data.get('limit_remaining') or 0):.4f}")
    else:
        lines.append("没有设额度上限")
    if data.get("is_free_tier"):
        lines.append("免费档（免费模型有每日次数限制）")
    return lines


async def _openai_billing(client: httpx.AsyncClient, profile: ModelProfile) -> list[str]:
    base_url = (profile.base_url or "").rstrip("/")
    subscription = await client.get(f"{base_url}/dashboard/billing/subscription", headers=_auth(profile))
    subscription.raise_for_status()
    hard_limit = float(subscription.json().get("hard_limit_usd") or 0)
    today = date.today()
    usage = await client.get(
        f"{base_url}/dashboard/billing/usage",
        params={"start_date": (today - timedelta(days=99)).isoformat(), "end_date": (today + timedelta(days=1)).isoformat()},
        headers=_auth(profile),
    )
    usage.raise_for_status()
    # 接口约定 total_usage 的单位是美分
    used = float(usage.json().get("total_usage") or 0) / 100
    return [f"额度 ${hard_limit:.2f}，已用 ${used:.2f}，剩余 ${max(hard_limit - used, 0):.2f}"]


def _auth(profile: ModelProfile) -> dict[str, str]:
    return {"Authorization": f"Bearer {profile.api_key or ''}"}


async def query_balance(profile: ModelProfile) -> BalanceReport:
    report = BalanceReport(provider=provider_label(profile), models=[profile.name])
    if not profile.api_key:
        report.error = "没配 API Key"
        return report
    host = _host(profile)
    if "bigmodel.cn" in host:
        report.error = "智谱没有公开的余额查询接口，请到 bigmodel.cn 控制台查看"
        return report
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            if "deepseek.com" in host:
                report.lines = await _deepseek_balance(client, profile)
            elif "openrouter.ai" in host:
                report.lines = await _openrouter_balance(client, profile)
            else:
                report.lines = await _openai_billing(client, profile)
    except httpx.HTTPStatusError as error:
        report.error = f"接口返回 {error.response.status_code}（这个网关可能不支持余额查询）"
        return report
    except Exception as error:
        report.error = f"{type(error).__name__}: {error}"
        return report
    report.ok = True
    return report


async def query_all_balances(profiles: list[ModelProfile]) -> list[dict[str, Any]]:
    """同一个接口 + 密钥只查一次，把用它的模型名列在一起。"""
    grouped: dict[tuple[str, str], list[ModelProfile]] = {}
    for profile in profiles:
        key = ((profile.base_url or "").rstrip("/"), profile.api_key or "")
        grouped.setdefault(key, []).append(profile)
    reports = []
    for same_account in grouped.values():
        report = await query_balance(same_account[0])
        report.models = [profile.name for profile in same_account]
        reports.append(report.to_dict())
    return reports
