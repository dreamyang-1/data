#!/usr/bin/env python3
"""生成第四批 500 轮专项题库（bank_v4.json）。

专项维度（用户指定）：
- 意图识别：间接表述意图（"带来多少收入""走货量""主要供到哪些省份"）
- 上下文追问：多轮指代追问（"排第一的省份里哪些医院""它一共买了多少"）
- 切换话题：会话中途换话题再回来
- 实体切换：换产品/经销商后指代回上一实体
- 指标切换：金额→数量→订单笔数→按月
- 改变问法：同题三种表述独立会话
- 不规范问法：口语化、无标点、实体简写（医院去前缀/经销商去"有限公司"）
"""
from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path

import pymysql

import bank_large as BL
import bank_v3 as B3

HERE = Path(__file__).resolve().parent
random.seed(20260903)

JP, JD, JH = BL.JOIN_PROD, BL.JOIN_DEALER, BL.JOIN_HOSP
esc = BL.sql_escape
q = B3.q
TARGET = 500


# ---------- 意图识别（间接表述） ----------

def intent_revenue(p: str) -> dict:
    return q(f"{p}给公司带来了多少收入？",
             f"SELECT ROUND(SUM(so.amount_with_tax),2) amt FROM sales_order so {JP} "
             f"WHERE p.product_name='{esc(p)}'", "money", money_col="amt")


def intent_volume(p: str) -> dict:
    return q(f"{p}的走货量怎么样？",
             f"SELECT ROUND(SUM(so.quantity),2) qty FROM sales_order so {JP} "
             f"WHERE p.product_name='{esc(p)}'", "count", count_field="qty")


def intent_top_hospital(p: str) -> dict:
    return q(f"哪家医院采购{p}最多？",
             f"SELECT h.hospital_name nm, ROUND(SUM(so.amount_with_tax),2) amt "
             f"FROM sales_order so {JP} {JH} WHERE p.product_name='{esc(p)}' "
             f"GROUP BY nm ORDER BY amt DESC LIMIT 1", "group", key_field="nm", val_field="amt")


def intent_prov_dist(p: str) -> dict:
    return q(f"{p}的货主要供到哪些省份？",
             f"SELECT so.business_province pv, ROUND(SUM(so.amount_with_tax),2) amt "
             f"FROM sales_order so {JP} WHERE p.product_name='{esc(p)}' AND so.business_province IS NOT NULL "
             f"GROUP BY pv ORDER BY amt DESC", "group", key_field="pv", val_field="amt")


def intent_overview(p: str) -> dict:
    return q(f"我想了解下{p}的整体销售情况。",
             f"SELECT ROUND(SUM(so.amount_with_tax),2) amt FROM sales_order so {JP} "
             f"WHERE p.product_name='{esc(p)}'", "money", money_col="amt")


# ---------- 不规范问法 ----------

def casual_nopunct(p: str) -> dict:
    return q(f"查下{p}的销售额",
             f"SELECT ROUND(SUM(so.amount_with_tax),2) amt FROM sales_order so {JP} "
             f"WHERE p.product_name='{esc(p)}'", "money", money_col="amt")


def casual_boss(p: str) -> dict:
    return q(f"老板要看{p}的销售数据",
             f"SELECT ROUND(SUM(so.amount_with_tax),2) amt FROM sales_order so {JP} "
             f"WHERE p.product_name='{esc(p)}'", "money", money_col="amt")


# ---------- 上下文追问（新模板，指代链） ----------

def m_followup_product(p: str, top_pv: str, top_hosp: str) -> list[dict]:
    base = f"p.product_name='{esc(p)}'"
    return [
        q(f"统计{p}在各省份的含税销售总额。",
          f"SELECT so.business_province pv, ROUND(SUM(so.amount_with_tax),2) amt "
          f"FROM sales_order so {JP} WHERE {base} AND so.business_province IS NOT NULL "
          f"GROUP BY pv ORDER BY amt DESC", "group", key_field="pv", val_field="amt"),
        q(f"排第一的省份里，有哪些医院在采购？",
          f"SELECT DISTINCT h.hospital_name nm FROM sales_order so {JP} {JH} "
          f"WHERE {base} AND so.business_province='{esc(top_pv)}' AND h.hospital_name IS NOT NULL "
          f"ORDER BY nm",
          "list", list_field="nm"),
        q("这些医院里采购金额最高的是哪家？",
          f"SELECT h.hospital_name nm, ROUND(SUM(so.amount_with_tax),2) amt "
          f"FROM sales_order so {JP} {JH} WHERE {base} AND so.business_province='{esc(top_pv)}' "
          f"AND h.hospital_name IS NOT NULL "
          f"GROUP BY nm ORDER BY amt DESC LIMIT 1", "group", key_field="nm", val_field="amt"),
        q("它一共买了多少钱的货？",
          f"SELECT ROUND(SUM(so.amount_with_tax),2) amt FROM sales_order so {JP} {JH} "
          f"WHERE {base} AND h.hospital_name='{esc(top_hosp)}'", "money", money_col="amt"),
    ]


def m_followup_dealer(d: str, top_hosp: str) -> list[dict]:
    base = f"d.dealer_name='{esc(d)}'"
    return [
        q(f"查询{d}的含税销售总额。",
          f"SELECT ROUND(SUM(so.amount_with_tax),2) amt FROM sales_order so {JD} WHERE {base}",
          "money", money_col="amt"),
        q("它主要供货哪些医院？",
          f"SELECT DISTINCT h.hospital_name nm FROM sales_order so {JD} {JH} "
          f"WHERE {base} AND h.hospital_name IS NOT NULL ORDER BY nm", "list", list_field="nm"),
        q("这些医院里采购金额最高的是哪家？",
          f"SELECT h.hospital_name nm, ROUND(SUM(so.amount_with_tax),2) amt "
          f"FROM sales_order so {JD} {JH} WHERE {base} AND h.hospital_name IS NOT NULL "
          f"GROUP BY nm ORDER BY amt DESC LIMIT 1", "group", key_field="nm", val_field="amt"),
        q("那家医院买了多少钱？",
          f"SELECT ROUND(SUM(so.amount_with_tax),2) amt FROM sales_order so {JD} {JH} "
          f"WHERE {base} AND h.hospital_name='{esc(top_hosp)}'", "money", money_col="amt"),
    ]


# ---------- 辅助 ----------

def top_hospital_in_province_of(conn, p: str, pv: str) -> str | None:
    rows, _ = BL.run(conn,
                     f"SELECT h.hospital_name FROM sales_order so {JP} {JH} "
                     f"WHERE p.product_name='{esc(p)}' AND so.business_province='{esc(pv)}' "
                     f"AND h.hospital_name IS NOT NULL "
                     f"GROUP BY h.hospital_name ORDER BY SUM(so.amount_with_tax) DESC LIMIT 1")
    return rows[0][0] if rows else None


def top_hospital_of_dealer_l(conn, d: str) -> str | None:
    rows, _ = BL.run(conn,
                     f"SELECT h.hospital_name FROM sales_order so {JD} {JH} "
                     f"WHERE d.dealer_name='{esc(d)}' AND h.hospital_name IS NOT NULL "
                     f"GROUP BY h.hospital_name ORDER BY SUM(so.amount_with_tax) DESC LIMIT 1")
    return rows[0][0] if rows else None


def main() -> None:
    conn = pymysql.connect(**BL.DB)
    ent = BL.fetch_entities(conn)
    products = ent["products"]
    dealers = ent["dealers"]
    hospitals = ent["hospitals"]
    provinces = ent["provinces"]

    scenarios: list[dict] = []
    sid = 0

    def add(name: str, turns: list[dict], split: bool = False) -> None:
        nonlocal sid
        if not turns:
            return
        sid += 1
        scenarios.append({"sid": f"W-{sid:03d}-{name}", "name": name,
                          "split_sessions": split, "turns": turns})

    # ===== A. 意图识别（48）=====
    for p in products[:12]:
        add("intent", [intent_revenue(p)], split=True)
        add("intent", [intent_volume(p)], split=True)
        add("intent", [intent_top_hospital(p)], split=True)
        add("intent", [intent_prov_dist(p)], split=True)

    # ===== B. 不规范问法（72）=====
    for p in products[:12]:
        add("casual", [B3.colloquial_money(p)], split=True)
        add("casual", [B3.colloquial_orders(p)], split=True)
        add("casual", [B3.colloquial_dealers(p)], split=True)
        add("casual", [casual_nopunct(p)], split=True)
        add("casual", [casual_boss(p)], split=True)
        add("casual", [B3.colloquial_province(p)], split=True)
    # 实体简写
    h_shorts = [(h, B3.hospital_short(h)) for h in hospitals]
    h_shorts = [(h, s) for h, s in h_shorts if s][:8]
    for h, s in h_shorts:
        add("abbrev_hosp", [q(f"查询{s}的含税销售总额。",
                              f"SELECT ROUND(SUM(so.amount_with_tax),2) amt FROM sales_order so {JH} "
                              f"WHERE h.hospital_name='{esc(h)}'", "money", money_col="amt")], split=True)
        add("abbrev_hosp", [q(f"{s}采购了多少笔订单？",
                              f"SELECT COUNT(DISTINCT so.order_key) n FROM sales_order so {JH} "
                              f"WHERE h.hospital_name='{esc(h)}'", "count", count_field="n")], split=True)
    d_shorts = [(d, B3.dealer_short(d)) for d in dealers]
    d_shorts = [(d, s) for d, s in d_shorts if s][:8]
    for d, s in d_shorts:
        add("abbrev_dealer", [q(f"查询{s}的含税销售总额。",
                                f"SELECT ROUND(SUM(so.amount_with_tax),2) amt FROM sales_order so {JD} "
                                f"WHERE d.dealer_name='{esc(d)}'", "money", money_col="amt")], split=True)

    # ===== C. 指标切换（40）=====
    for p in products[12:20]:
        add("m_metric", B3.m_metric_switch(p))

    # ===== D. 维度切换（30）=====
    add("m_dim_global", B3.m_dim_switch_global())
    for p in products[20:23]:
        add("m_dim_product", B3.m_dim_switch_product(p))
    for h in hospitals[20:22]:
        add("m_dim_hosp", B3.m_dim_switch_hospital(h))

    # ===== E. 实体切换（44）=====
    for i in range(8):
        p1, p2, p3 = products[i], products[i + 1], products[i + 2]
        add("m_entity", B3.m_entity_switch(p1, p2, p3))
    for i in range(3):
        add("m_entity_dealer", B3.m_entity_switch_dealer(dealers[i], dealers[i + 1]))

    # ===== F. 过滤追加与撤销（40）=====
    cnt = 0
    for p in products[24:]:
        if cnt >= 8:
            break
        pv2 = B3.top_provinces_of(conn, p, 2)
        if len(pv2) < 2:
            continue
        add("m_filter", B3.m_filter_add(p, pv2))
        cnt += 1

    # ===== G. 排序与序数指代（48）=====
    topd = B3.top_dealers_global(conn, 5)
    topp = B3.top_products_global(conn, 5)
    for _ in range(8):
        add("m_sort_ref", B3.m_sort_ref(topd))
    for _ in range(4):
        add("m_sort_ref_p", B3.m_sort_ref_product(topp))

    # ===== H. 话题切换（25）=====
    for i in range(5):
        add("m_topic", BL.multi_topic_switch(products[i + 4], products[i + 10]))

    # ===== I. 下钻（45）=====
    for p in products[28:31]:
        add("m_drill_p", BL.multi_product_drilldown(p))
    for pv in provinces[5:7]:
        add("m_drill_pv", BL.multi_province_drilldown(pv))
    for h in hospitals[22:24]:
        add("m_drill_h", BL.multi_hospital_drilldown(h))
    for d in dealers[20:22]:
        add("m_drill_d", BL.multi_dealer_drilldown(d))

    # ===== J. 换问法一致性（36）=====
    for p in products[35:47]:
        for one in B3.phrasing_trio(p):
            add("phrasing", one, split=True)

    # ===== K. 上下文追问·指代链（40）=====
    cnt = 0
    for p in products:
        if cnt >= 6:
            break
        pv = B3.top_province_of_product(conn, p)
        if not pv:
            continue
        hp = top_hospital_in_province_of(conn, p, pv)
        if not hp:
            continue
        add("m_followup", m_followup_product(p, pv, hp))
        cnt += 1
    cnt = 0
    for d in dealers:
        if cnt >= 4:
            break
        hp = top_hospital_of_dealer_l(conn, d)
        if not hp:
            continue
        add("m_followup_d", m_followup_dealer(d, hp))
        cnt += 1

    # ===== 补齐到 500 =====
    total = sum(len(s["turns"]) for s in scenarios)
    print(f"核心轮数: {total}")
    fi = 0
    while total < TARGET:
        p = products[fi % len(products)]
        d = dealers[fi % len(dealers)]
        h = hospitals[fi % len(hospitals)]
        mode = fi % 4
        if mode == 0:
            add("fill", [BL.t_product_money(p)], split=True)
        elif mode == 1:
            add("fill", [intent_volume(p)], split=True)
        elif mode == 2:
            add("fill", [BL.t_hospital_money(h)], split=True)
        else:
            add("fill", [BL.t_dealer_money(d)], split=True)
        total += 1
        fi += 1

    # 生成标准答案
    turns_total = 0
    bank = {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "meta": {"target": TARGET, "scripts": ["意图识别", "不规范问法", "指标切换", "维度切换",
                                                   "实体切换", "过滤追加撤销", "排序指代", "话题切换",
                                                   "下钻", "换问法", "上下文追问指代链"]},
            "scenarios": []}
    for s in scenarios:
        s2 = {"sid": s["sid"], "name": s["name"],
              "split_sessions": s["split_sessions"], "turns": []}
        for i, t in enumerate(s["turns"], 1):
            std = BL.build_std(t, conn)
            s2["turns"].append({"tid": f"{s['sid']}-T{i}", "question": t["question"],
                                "check_type": t["check_type"], "std": std})
            turns_total += 1
        bank["scenarios"].append(s2)
    bank["total_turns"] = turns_total

    out = HERE / "bank_v4.json"
    out.write_text(json.dumps(bank, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"场景数: {len(bank['scenarios'])}，总轮数: {turns_total}，已写入 {out.name}")
    conn.close()


if __name__ == "__main__":
    main()
