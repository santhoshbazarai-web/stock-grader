"""Builds the live JobContext: DB, Redis, rate limiter, providers and the dataset router."""

from redis import Redis

from app.alerts.telegram import build_notifier
from app.core.config import AppConfig, Provider
from app.core.rate_limiter import RateLimiter
from app.core.security import TokenCipher
from app.core.settings import Settings
from app.data.broker_tokens import BrokerTokenStore
from app.data.gaps import DbGapRecorder
from app.data.providers.fyers import build_fyers_provider
from app.data.providers.kite import build_kite_provider
from app.data.providers.kite_instruments import RedisInstrumentStore
from app.data.providers.nse import build_nse_provider
from app.data.providers.screener_import import ScreenerProvider
from app.data.providers.yf import build_yfinance_provider
from app.data.raw_store import RawStore
from app.data.router import DataRouter
from app.db.enums import Broker
from app.db.session import get_session_factory
from app.jobs.runner import JobContext


def build_context(settings: Settings, config: AppConfig) -> JobContext:
    redis = Redis.from_url(settings.redis_url)
    session_factory = get_session_factory()
    limiter = RateLimiter(redis, config.providers.rate_limits)
    tokens = BrokerTokenStore(session_factory, TokenCipher(settings.fernet_key.get_secret_value()))
    pc = config.providers
    raw_store = RawStore(settings.raw_data_dir)
    candidates: dict[Provider, object | None] = {
        Provider.FYERS: build_fyers_provider(
            settings.fyers_app_id, lambda: tokens.get_valid(Broker.FYERS), pc, limiter
        ),
        Provider.KITE: build_kite_provider(
            settings.kite_api_key,
            lambda: tokens.get_valid(Broker.KITE),
            pc,
            RedisInstrumentStore(redis),
            limiter,
        ),
        Provider.YFINANCE: build_yfinance_provider(pc, limiter),
        Provider.NSE: build_nse_provider(pc, limiter, raw_store),
        Provider.SCREENER: ScreenerProvider(session_factory),
    }
    providers = {k: v for k, v in candidates.items() if v is not None}
    gaps = DbGapRecorder(session_factory)
    router = DataRouter(providers, pc, limiter=limiter, gaps=gaps)
    notifier = build_notifier(settings, config.jobs.alerts.telegram_timeout_s)
    return JobContext(
        config, session_factory, router, redis, gaps, notifier=notifier, raw_store=raw_store
    )
