"""Seed deterministic, replaceable history for data-agent acceptance tests.

Only rows whose primary keys start with ``DAH_`` are replaced. Existing
business/demo rows remain untouched. The generated period is 2025-01 through
2026-06; existing 2026-07 rows then provide a continuous 19-month series.
"""
from __future__ import annotations

import calendar
import os
import random
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pymysql


def load_root_env() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def month_range(start_year: int, start_month: int, count: int):
    year, month = start_year, start_month
    for _ in range(count):
        yield year, month
        month += 1
        if month == 13:
            year += 1
            month = 1


def main() -> None:
    load_root_env()
    connection = pymysql.connect(
        host=os.environ["MYSQL_137_HOST"],
        port=int(os.environ["MYSQL_137_PORT"]),
        user=os.environ["MYSQL_137_USER"],
        password=os.environ["MYSQL_137_PASSWORD"],
        database="sql_trns_test",
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )
    rng = random.Random(20260825)
    orders: list[tuple] = []
    items: list[tuple] = []
    payments: list[tuple] = []
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT customer_id FROM customer_info ORDER BY customer_id")
            customers = [row["customer_id"] for row in cursor.fetchall()]
            cursor.execute("SELECT shop_id FROM shop_info ORDER BY shop_id")
            shops = [row["shop_id"] for row in cursor.fetchall()]
            cursor.execute(
                "SELECT goods_id, goods_name, goods_category, unit_price "
                "FROM goods_info ORDER BY goods_id"
            )
            goods = cursor.fetchall()
            if not customers or not shops or not goods:
                raise RuntimeError("customer_info, shop_info and goods_info must contain seed dimensions")

            # Idempotent cleanup in foreign-key order.
            cursor.execute("DELETE FROM payment_record WHERE payment_id LIKE 'DAH\\_%'")
            cursor.execute("DELETE FROM order_item_detail WHERE item_id LIKE 'DAH\\_%'")
            cursor.execute("DELETE FROM order_info WHERE order_id LIKE 'DAH\\_%'")

            for month_index, (year, month) in enumerate(month_range(2025, 1, 18)):
                # Growing baseline, seasonality and one intentional anomaly
                # provide useful trend/anomaly/root-cause test material.
                order_count = 72 + month_index * 2
                seasonal = Decimal("1.18") if month in (6, 11, 12) else Decimal("1.00")
                anomaly = Decimal("0.52") if (year, month) == (2026, 4) else Decimal("1.00")
                value_factor = seasonal * anomaly
                last_day = calendar.monthrange(year, month)[1]
                for index in range(order_count):
                    suffix = f"{year}{month:02d}_{index:04d}"
                    order_id = f"DAH_O_{suffix}"
                    customer_id = customers[(index * 7 + month_index) % len(customers)]
                    # Shift shop/channel mix over time so attribution has a
                    # measurable dimensional contribution.
                    shop_id = shops[(index + month_index * 3) % len(shops)]
                    product = goods[(index * 5 + month_index) % len(goods)]
                    quantity = 1 + (index % 3)
                    day = 1 + ((index * 7 + month_index) % last_day)
                    hour = 9 + (index % 12)
                    pay_time = datetime(year, month, day, hour, (index * 11) % 60, 0)
                    unit_price = Decimal(str(product["unit_price"]))
                    amount = (unit_price * quantity * value_factor).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    refunded = index % 23 == 0
                    refund_amount = (amount * Decimal("0.20")).quantize(Decimal("0.01")) if refunded else Decimal("0")
                    channel = ("01", "02", "03", "04")[(index + month_index) % 4]
                    status = 5 if refunded else (1, 2, 3)[index % 3]
                    payment_method = ("01", "02", "03", "04")[(index * 3 + month_index) % 4]

                    orders.append((
                        order_id, customer_id, shop_id, pay_time.date(), pay_time,
                        status, amount, refund_amount, channel, pay_time,
                    ))
                    items.append((
                        f"DAH_I_{suffix}", order_id, product["goods_id"],
                        product["goods_name"], product["goods_category"], quantity,
                        unit_price, amount, pay_time.date(),
                    ))
                    payments.append((
                        f"DAH_P_{suffix}", order_id, amount, payment_method,
                        pay_time, 1, f"DAH_TX_{suffix}", int(refunded), pay_time,
                    ))

            cursor.executemany(
                "INSERT INTO order_info "
                "(order_id,customer_id,shop_id,order_date,pay_time,order_status,"
                "pay_amount,refund_amount,channel_type,create_time) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                orders,
            )
            cursor.executemany(
                "INSERT INTO order_item_detail "
                "(item_id,order_id,goods_id,goods_name,goods_category,quantity,"
                "unit_price,item_amount,order_date) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                items,
            )
            cursor.executemany(
                "INSERT INTO payment_record "
                "(payment_id,order_id,payment_amount,payment_method,payment_time,"
                "payment_status,transaction_id,refund_flag,create_time) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                payments,
            )
        connection.commit()
        print(f"seeded_orders={len(orders)} seeded_items={len(items)} seeded_payments={len(payments)}")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
