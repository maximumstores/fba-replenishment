"""Настройки расчёта. Все значения можно переопределить переменными окружения (.env).

ВАЖНО: сроки по каналам (lead_*) и сроки прихода inbound (*_days) сейчас ДОПУЩЕНИЯ,
а не утверждённые значения. Когда появятся настоящие — меняются здесь или в .env,
остальное пересчитывается само.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# Порядок = приоритет по цене: сначала самый дешёвый канал, потом дороже.
CHANNELS = ("AWD", "WAREHOUSE", "SEA", "AIR")
CHANNEL_RU = {
    "AWD": "AWD → FBA",
    "WAREHOUSE": "Склад → FBA",
    "SEA": "Море",
    "AIR": "Воздух",
}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw.replace(",", "."))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "да")


@dataclass(frozen=True)
class Settings:
    # Сколько дней от решения до появления товара на FBA по каналу (ДОПУЩЕНИЯ)
    lead_awd: float = 7.0
    lead_warehouse: float = 14.0
    lead_sea: float = 30.0   # факт по отправкам Виктории (US): медиана ETD → доставка ≈ 30 дн. (FAST SEA 32, Matson 26, MAX/EXX 24, Regular 39)
    lead_air: float = 10.0   # допущение: воздухом ещё ни разу не возили, данных нет

    # Когда придёт то, что уже едет на FBA (в днях от сегодня) (ДОПУЩЕНИЯ)
    receiving_days: float = 3.0   # уже на складе Amazon, принимается
    shipped_days: float = 14.0    # отправлено, в пути
    working_days: float = 21.0    # поставка создана, но не отправлена
    include_working: bool = False  # считать ли "working" надёжным приходом (по умолчанию нет)

    # Скорость продаж = weight_7d * (за 7 дн) + (1 - weight_7d) * (за 30 дн)
    weight_7d: float = 0.5

    # На сколько дней продаж после прихода нужно пополнять
    target_cover_days: float = 45.0
    # За сколько дней до обнуления (сверх срока AWD) начинать показывать как "PLAN"
    plan_buffer_days: float = 14.0
    horizon_days: int = 90

    # Партии из Orders-Stock: дата там — крайний срок приёмки, задержки в ней не видны,
    # поэтому добавляем запас к ETA (и не раньше сегодня).
    batch_delay_days: float = 14.0

    # Прогноз планера как резерв скорости: "off" | "fallback" (только если факт = 0) | "floor" (не ниже плана)
    plan_mode: str = "off"

    only_active: bool = True       # только active_us из справочника
    abc_a: float = 0.80            # граница класса A по накопленной доле продаж 30 дн
    abc_b: float = 0.95            # граница класса B

    @property
    def leads(self) -> dict[str, float]:
        return {"AWD": self.lead_awd, "WAREHOUSE": self.lead_warehouse, "SEA": self.lead_sea, "AIR": self.lead_air}

    @classmethod
    def from_env(cls) -> "Settings":
        d = cls()
        return cls(
            lead_awd=_env_float("LEAD_AWD", d.lead_awd),
            lead_warehouse=_env_float("LEAD_WAREHOUSE", d.lead_warehouse),
            lead_sea=_env_float("LEAD_SEA", d.lead_sea),
            lead_air=_env_float("LEAD_AIR", d.lead_air),
            receiving_days=_env_float("RECEIVING_DAYS", d.receiving_days),
            shipped_days=_env_float("SHIPPED_DAYS", d.shipped_days),
            working_days=_env_float("WORKING_DAYS", d.working_days),
            include_working=_env_bool("INCLUDE_WORKING", d.include_working),
            weight_7d=_env_float("WEIGHT_7D", d.weight_7d),
            target_cover_days=_env_float("TARGET_COVER_DAYS", d.target_cover_days),
            plan_buffer_days=_env_float("PLAN_BUFFER_DAYS", d.plan_buffer_days),
            horizon_days=int(_env_float("HORIZON_DAYS", d.horizon_days)),
            batch_delay_days=_env_float("BATCH_DELAY_DAYS", d.batch_delay_days),
            plan_mode=(os.getenv("PLAN_MODE") or d.plan_mode).strip().lower(),
            only_active=_env_bool("ONLY_ACTIVE", d.only_active),
            abc_a=_env_float("ABC_A", d.abc_a),
            abc_b=_env_float("ABC_B", d.abc_b),
        )
