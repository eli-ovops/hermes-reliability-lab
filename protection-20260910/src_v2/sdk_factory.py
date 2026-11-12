"""OKX SDK factory — 构建客户端、验证账户模式、校验服务器时间。

只接 demo 或实盘由调用方显式传入 demo 标志，本模块不读凭证文件。
服务器时间偏差超过阈值会拒绝（避免 50102 签名过期）。
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib

from src_v2.okx_gateway import GatewayError, OKXGateway

DUAL_POSITION_MODE = "long_short_mode"
DEFAULT_MAX_SKEW_SECONDS = 30.0


def build_gateway(
    *,
    api_key: str,
    api_secret: str,
    passphrase: str,
    demo: bool,
    proxy: str | None = None,
) -> OKXGateway:
    """构建 python-okx 客户端并封装为 OKXGateway。

    flag='1' 为 demo（模拟盘），flag='0' 为实盘。
    """
    import okx.Account as Account
    import okx.MarketData as MarketData
    import okx.PublicData as PublicData
    import okx.Trade as Trade

    flag = "1" if demo else "0"
    kwargs = {"flag": flag, "debug": False}
    if proxy:
        kwargs["proxy"] = proxy
    account = Account.AccountAPI(api_key, api_secret, passphrase, **kwargs)
    trade = Trade.TradeAPI(api_key, api_secret, passphrase, **kwargs)
    market = MarketData.MarketAPI(**kwargs)
    public = PublicData.PublicAPI(**kwargs)
    return OKXGateway(
        account_api=account,
        trade_api=trade,
        market_api=market,
        public_api=public,
    )


def server_time_skew_seconds(
    market_api, *, max_skew: float = DEFAULT_MAX_SKEW_SECONDS
) -> float:
    """获取 OKX 服务器时间，返回 本地时间 - 服务器时间 的秒差。

    python-okx SDK 用本地时钟签名，若偏差过大 OKX 会返回 50102。
    这里用公共端点 /api/v5/public/time（SDK 私有方法 _get_timestamp）
    校准偏差，超过阈值即抛错，避免带病下单。
    """
    response = market_api._get_timestamp()  # noqa: SLF001 - SDK 唯一服务器时间入口
    if not response:
        raise GatewayError("无法获取 OKX 服务器时间")
    try:
        server = datetime.fromisoformat(response.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GatewayError("服务器时间格式无效") from exc
    local = datetime.now(timezone.utc)
    skew = (local - server).total_seconds()
    if abs(skew) > max_skew:
        raise GatewayError(
            f"本地时间与服务器时间偏差 {skew:.1f} 秒，超过 {max_skew} 秒，"
            "请先同步系统时间（NTP）再下单"
        )
    return skew


def verify_account_config(account_api) -> dict:
    """验证账户为双向持仓模式，返回配置摘要。"""
    response = account_api.get_account_config()
    if not isinstance(response, dict) or response.get("code") != "0":
        raise GatewayError("无法读取账户配置")
    data = response.get("data") or []
    if not data:
        raise GatewayError("账户配置为空")
    config = data[0]
    pos_mode = config.get("posMode")
    if pos_mode != DUAL_POSITION_MODE:
        raise GatewayError(
            f"账户不是双向持仓模式（当前 {pos_mode}），"
            "v2 需要 long_short_mode 才能按方向独立开多空"
        )
    main_uid = str(config.get("mainUid") or "").strip()
    uid = str(config.get("uid") or "").strip()
    if not main_uid or not uid:
        raise GatewayError("账户配置缺少可验证的账户身份")
    account_fingerprint = hashlib.sha256(
        f"{main_uid}:{uid}".encode("utf-8")
    ).hexdigest()
    return {
        "pos_mode": pos_mode,
        "acct_level": config.get("acctLv"),
        "auto_loan": config.get("autoLoan"),
        "account_fingerprint": account_fingerprint,
    }
