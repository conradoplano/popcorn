"""
The AI (OpenAI), the same way Chef uses it: what it costs, and the daily limits that keep it affordable.

Every call is written to the AIUsage ledger with its cost. Before an import uses AI, the person's
spending today is checked against their limit (AI_DAILY_LIMIT_USD, or their own), and everyone's
against AI_GLOBAL_DAILY_LIMIT_USD. AI is only used once an admin has approved the person.
"""
import logging
from datetime import datetime, time
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from .models import AIUsage

logger = logging.getLogger(__name__)

# USD per million tokens: (input, output).
PRICES = {
    "gpt-5.4": (Decimal("2.50"), Decimal("15.00")),
    "gpt-5.4-mini": (Decimal("0.75"), Decimal("4.50")),
    "gpt-5.4-nano": (Decimal("0.20"), Decimal("1.25")),
    "gpt-5.4-pro": (Decimal("30.00"), Decimal("180.00")),
    "gpt-6.1-sol": (Decimal("2"), Decimal("10")),
    "gpt-6-astra": (Decimal("10"), Decimal("50")),
}


class AIError(Exception):
    pass


def get_client():
    import openai

    return openai.OpenAI(api_key=settings.OPENAI_API_KEY, max_retries=1, timeout=120)


def today_start():
    return timezone.make_aware(datetime.combine(timezone.localdate(), time.min))


def spent(since=None, **filters):
    """Total cost in USD, e.g. spent(today_start(), user=u)."""
    usage = AIUsage.objects.filter(**filters)
    if since:
        usage = usage.filter(created_at__gte=since)
    return usage.aggregate(total=Sum("cost"))["total"] or Decimal(0)


def daily_limit(user):
    return user.ai_daily_limit if user.ai_daily_limit is not None else settings.AI_DAILY_LIMIT_USD


def blocked(user):
    """Why the person can't use AI right now, or "" if they can."""
    if not settings.OPENAI_API_KEY:
        return "AI isn't set up (OPENAI_API_KEY is missing)."
    if not (user.ai_approved or user.is_staff):
        return "AI is switched on for you once an admin has approved it."
    limit = daily_limit(user)
    if limit <= 0:
        return "AI is switched off for you."
    if spent(today_start(), user=user) >= limit:
        return f"You've used today's AI budget (${limit:.2f}). It starts again tomorrow."
    if spent(today_start()) >= settings.AI_GLOBAL_DAILY_LIMIT_USD:
        return "The app has used its AI budget for today."
    return ""


def prices_for(model):
    """(input, output) USD per million tokens. Also finds dated versions ("gpt-5.4-mini-2026-03-17")."""
    model = (model or "").strip().lower()
    if model in PRICES:
        return PRICES[model]
    known = [name for name in PRICES if model.startswith(name + "-")]
    return PRICES[max(known, key=len)] if known else None


def cost(model, input_tokens, output_tokens):
    prices = prices_for(model)
    if not prices:
        logger.warning("No price known for AI model %r; its cost is recorded as 0. Add it to PRICES.", model)
        return Decimal(0)
    return ((input_tokens * prices[0] + output_tokens * prices[1]) / Decimal(1_000_000)).quantize(Decimal("0.0001"))


def record(user, kind, response):
    """Writes one response to the ledger."""
    tokens = response.usage
    input_tokens = (tokens.input_tokens or 0) if tokens else 0
    output_tokens = (tokens.output_tokens or 0) if tokens else 0
    # The model that answered, e.g. "gpt-5.4-mini-2026-03-17"; the setting if the response doesn't say.
    model = getattr(response, "model", None)
    model = model if isinstance(model, str) and model else settings.AI_MODEL
    return AIUsage.objects.create(
        user=user, kind=kind, model=model, input_tokens=input_tokens, output_tokens=output_tokens,
        cost=cost(model, input_tokens, output_tokens),
    )
