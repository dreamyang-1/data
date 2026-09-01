-- ================================================================
-- 数据分析智能体：电商领域端到端测试数据
-- 适用：MySQL 8.0+（MySQL 5.7 也可执行）
-- 专用测试库：ecommerce_agent_test
--
-- 覆盖场景：
--   指标查询、明细查询、趋势、同比/环比、占比、异常、归因、预测、
--   数据质量、报表，以及按地区/渠道/店铺/品类/商品/会员等级下钻。
--
-- 安全说明：脚本不会 DROP DATABASE，只会重建本专用测试库中的同名表。
-- 可重复执行，每次得到完全相同的模拟数据。
-- ================================================================

CREATE DATABASE IF NOT EXISTS ecommerce_agent_test
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE ecommerce_agent_test;

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

DROP VIEW IF EXISTS v_daily_sales;
DROP VIEW IF EXISTS v_monthly_sales;
DROP VIEW IF EXISTS v_order_wide;
DROP TABLE IF EXISTS refund_record;
DROP TABLE IF EXISTS payment_record;
DROP TABLE IF EXISTS order_item_detail;
DROP TABLE IF EXISTS order_info;
DROP TABLE IF EXISTS inventory_snapshot;
DROP TABLE IF EXISTS promotion_activity;
DROP TABLE IF EXISTS goods_info;
DROP TABLE IF EXISTS goods_category;
DROP TABLE IF EXISTS customer_info;
DROP TABLE IF EXISTS shop_info;

SET FOREIGN_KEY_CHECKS = 1;

-- 1. 店铺：用于地区、门店、开店时间等维度分析
CREATE TABLE shop_info (
  shop_id        BIGINT PRIMARY KEY,
  shop_name      VARCHAR(100) NOT NULL,
  region_code    VARCHAR(20) NOT NULL,
  region_name    VARCHAR(50) NOT NULL,
  city_name      VARCHAR(50) NOT NULL,
  shop_level     VARCHAR(20) NOT NULL,
  open_date      DATE NOT NULL,
  status         TINYINT NOT NULL DEFAULT 1,
  create_time    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_shop_region (region_code, city_name)
) ENGINE=InnoDB COMMENT='电商店铺维度表';

-- 2. 客户：用于会员等级、地区、新老客分析
CREATE TABLE customer_info (
  customer_id    BIGINT PRIMARY KEY,
  customer_name  VARCHAR(100) NOT NULL,
  gender         VARCHAR(10) NOT NULL,
  birth_date     DATE NOT NULL,
  member_level   VARCHAR(20) NOT NULL,
  province_name  VARCHAR(50) NOT NULL,
  city_name      VARCHAR(50) NOT NULL,
  register_date  DATE NOT NULL,
  status         TINYINT NOT NULL DEFAULT 1,
  create_time    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_customer_level (member_level),
  KEY idx_customer_region (province_name, city_name)
) ENGINE=InnoDB COMMENT='客户信息表';

-- 3. 商品类目
CREATE TABLE goods_category (
  category_id          BIGINT PRIMARY KEY,
  category_code        VARCHAR(30) NOT NULL UNIQUE,
  category_name        VARCHAR(50) NOT NULL,
  parent_category_id   BIGINT NULL,
  category_level       INT NOT NULL DEFAULT 1,
  status               TINYINT NOT NULL DEFAULT 1
) ENGINE=InnoDB COMMENT='商品类目表';

-- 4. 商品：包含成本、售价、品牌，用于毛利和商品归因
CREATE TABLE goods_info (
  goods_id        BIGINT PRIMARY KEY,
  goods_code      VARCHAR(30) NOT NULL UNIQUE,
  goods_name      VARCHAR(100) NOT NULL,
  goods_alias     VARCHAR(200) NULL COMMENT '别名，多个值用逗号分隔',
  category_id     BIGINT NOT NULL,
  brand_name      VARCHAR(50) NOT NULL,
  cost_price      DECIMAL(12,2) NOT NULL,
  sale_price      DECIMAL(12,2) NOT NULL,
  launch_date     DATE NOT NULL,
  status          TINYINT NOT NULL DEFAULT 1,
  CONSTRAINT fk_goods_category FOREIGN KEY (category_id) REFERENCES goods_category(category_id),
  KEY idx_goods_category (category_id),
  KEY idx_goods_brand (brand_name)
) ENGINE=InnoDB COMMENT='商品信息表';

-- 5. 促销活动：用于分析活动对销售的影响
CREATE TABLE promotion_activity (
  activity_id       BIGINT PRIMARY KEY,
  activity_name     VARCHAR(100) NOT NULL,
  activity_type     VARCHAR(30) NOT NULL,
  discount_rate     DECIMAL(6,4) NOT NULL DEFAULT 1.0000,
  start_time        DATETIME NOT NULL,
  end_time          DATETIME NOT NULL,
  budget_amount     DECIMAL(14,2) NOT NULL,
  status            TINYINT NOT NULL DEFAULT 1,
  KEY idx_activity_time (start_time, end_time)
) ENGINE=InnoDB COMMENT='促销活动表';

-- 6. 订单主表：订单级指标和公共维度
CREATE TABLE order_info (
  order_id             BIGINT PRIMARY KEY,
  order_no             VARCHAR(40) NOT NULL UNIQUE,
  customer_id          BIGINT NOT NULL,
  shop_id              BIGINT NOT NULL,
  activity_id          BIGINT NULL,
  order_channel        VARCHAR(20) NOT NULL COMMENT 'APP/小程序/网页/直播',
  order_status         VARCHAR(20) NOT NULL COMMENT 'PAID/COMPLETED/REFUNDED/CANCELLED',
  order_time           DATETIME NOT NULL,
  pay_time             DATETIME NULL,
  total_goods_amount   DECIMAL(14,2) NOT NULL,
  discount_amount      DECIMAL(14,2) NOT NULL DEFAULT 0,
  freight_amount       DECIMAL(12,2) NOT NULL DEFAULT 0,
  pay_amount           DECIMAL(14,2) NOT NULL,
  cost_amount          DECIMAL(14,2) NOT NULL,
  province_name        VARCHAR(50) NOT NULL,
  city_name            VARCHAR(50) NOT NULL,
  create_time          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_order_customer FOREIGN KEY (customer_id) REFERENCES customer_info(customer_id),
  CONSTRAINT fk_order_shop FOREIGN KEY (shop_id) REFERENCES shop_info(shop_id),
  CONSTRAINT fk_order_activity FOREIGN KEY (activity_id) REFERENCES promotion_activity(activity_id),
  KEY idx_order_pay_time (pay_time),
  KEY idx_order_customer_time (customer_id, order_time),
  KEY idx_order_shop_time (shop_id, order_time),
  KEY idx_order_channel_time (order_channel, order_time),
  KEY idx_order_status_time (order_status, order_time)
) ENGINE=InnoDB COMMENT='订单主表';

-- 7. 订单商品明细：品类、商品、销量、毛利分析的事实表
CREATE TABLE order_item_detail (
  item_id          BIGINT PRIMARY KEY,
  order_id         BIGINT NOT NULL,
  goods_id         BIGINT NOT NULL,
  quantity         INT NOT NULL,
  unit_price       DECIMAL(12,2) NOT NULL,
  item_amount      DECIMAL(14,2) NOT NULL,
  discount_amount  DECIMAL(14,2) NOT NULL DEFAULT 0,
  actual_amount    DECIMAL(14,2) NOT NULL,
  cost_amount      DECIMAL(14,2) NOT NULL,
  CONSTRAINT fk_item_order FOREIGN KEY (order_id) REFERENCES order_info(order_id),
  CONSTRAINT fk_item_goods FOREIGN KEY (goods_id) REFERENCES goods_info(goods_id),
  KEY idx_item_order (order_id),
  KEY idx_item_goods (goods_id)
) ENGINE=InnoDB COMMENT='订单商品明细表';

-- 8. 支付记录
CREATE TABLE payment_record (
  payment_id       BIGINT PRIMARY KEY,
  payment_no       VARCHAR(40) NOT NULL UNIQUE,
  order_id         BIGINT NOT NULL,
  payment_method   VARCHAR(20) NOT NULL COMMENT 'WECHAT/ALIPAY/CARD',
  payment_status   VARCHAR(20) NOT NULL,
  payment_amount   DECIMAL(14,2) NOT NULL,
  payment_time     DATETIME NOT NULL,
  CONSTRAINT fk_payment_order FOREIGN KEY (order_id) REFERENCES order_info(order_id),
  KEY idx_payment_order (order_id),
  KEY idx_payment_time (payment_time)
) ENGINE=InnoDB COMMENT='支付记录表';

-- 9. 退款记录：用于退款率、退款金额、退款原因分析
CREATE TABLE refund_record (
  refund_id        BIGINT PRIMARY KEY,
  refund_no        VARCHAR(40) NOT NULL UNIQUE,
  order_id         BIGINT NOT NULL,
  goods_id         BIGINT NOT NULL,
  refund_type      VARCHAR(20) NOT NULL,
  refund_reason    VARCHAR(100) NOT NULL,
  refund_amount    DECIMAL(14,2) NOT NULL,
  apply_time       DATETIME NOT NULL,
  complete_time    DATETIME NULL,
  refund_status    VARCHAR(20) NOT NULL,
  CONSTRAINT fk_refund_order FOREIGN KEY (order_id) REFERENCES order_info(order_id),
  CONSTRAINT fk_refund_goods FOREIGN KEY (goods_id) REFERENCES goods_info(goods_id),
  KEY idx_refund_order (order_id),
  KEY idx_refund_time (complete_time),
  KEY idx_refund_reason (refund_reason)
) ENGINE=InnoDB COMMENT='退款记录表';

-- 10. 月末库存快照：用于库存趋势、缺货和滞销分析
CREATE TABLE inventory_snapshot (
  snapshot_date      DATE NOT NULL,
  shop_id            BIGINT NOT NULL,
  goods_id           BIGINT NOT NULL,
  available_quantity INT NOT NULL,
  locked_quantity    INT NOT NULL DEFAULT 0,
  safety_stock       INT NOT NULL DEFAULT 10,
  inventory_amount   DECIMAL(14,2) NOT NULL,
  PRIMARY KEY (snapshot_date, shop_id, goods_id),
  CONSTRAINT fk_inventory_shop FOREIGN KEY (shop_id) REFERENCES shop_info(shop_id),
  CONSTRAINT fk_inventory_goods FOREIGN KEY (goods_id) REFERENCES goods_info(goods_id),
  KEY idx_inventory_goods (goods_id, snapshot_date)
) ENGINE=InnoDB COMMENT='库存月末快照表';

INSERT INTO shop_info VALUES
  (1,'华东旗舰店','EAST','华东','上海','S','2023-01-01',1,NOW()),
  (2,'华北中心店','NORTH','华北','北京','A','2023-04-15',1,NOW()),
  (3,'华南体验店','SOUTH','华南','深圳','A','2023-08-01',1,NOW()),
  (4,'西南直营网店','WEST','西南','成都','B','2024-02-01',1,NOW());

INSERT INTO customer_info VALUES
  (1,'客户001','女','1992-03-12','黄金','上海','上海','2024-01-02',1,NOW()),
  (2,'客户002','男','1988-08-23','白金','北京','北京','2024-01-15',1,NOW()),
  (3,'客户003','女','1998-11-05','普通','广东','深圳','2024-02-03',1,NOW()),
  (4,'客户004','男','1995-06-17','黄金','四川','成都','2024-02-28',1,NOW()),
  (5,'客户005','女','1985-01-21','钻石','浙江','杭州','2024-03-19',1,NOW()),
  (6,'客户006','男','2000-09-09','普通','江苏','南京','2024-04-08',1,NOW()),
  (7,'客户007','女','1990-12-30','白金','山东','青岛','2024-05-20',1,NOW()),
  (8,'客户008','男','1997-04-14','黄金','湖北','武汉','2024-06-11',1,NOW()),
  (9,'客户009','女','1993-07-07','普通','福建','厦门','2024-07-07',1,NOW()),
  (10,'客户010','男','1989-10-18','钻石','重庆','重庆','2024-08-18',1,NOW()),
  (11,'客户011','女','2001-02-26','普通','河南','郑州','2024-09-01',1,NOW()),
  (12,'客户012','男','1994-05-16','黄金','陕西','西安','2024-10-12',1,NOW());

INSERT INTO goods_category VALUES
  (1,'DIGITAL','数码家电',NULL,1,1),
  (2,'HOME','家居生活',NULL,1,1),
  (3,'SPORT','运动户外',NULL,1,1),
  (4,'FASHION','服饰箱包',NULL,1,1),
  (5,'FOOD','食品饮料',NULL,1,1);

INSERT INTO goods_info VALUES
  (1,'G001','无线蓝牙耳机','蓝牙耳机,无线耳机',1,'声动',120.00,299.00,'2024-01-01',1),
  (2,'G002','智能手表','运动手表,智能腕表',1,'智联',260.00,599.00,'2024-01-01',1),
  (3,'G003','机械键盘','游戏键盘,电竞键盘',1,'极客',180.00,399.00,'2024-02-01',1),
  (4,'G004','办公椅','人体工学椅,电脑椅',2,'宜居',420.00,899.00,'2024-01-15',1),
  (5,'G005','保温杯','水杯,随行杯',2,'暖行',35.00,99.00,'2024-03-01',1),
  (6,'G006','瑜伽垫','健身垫,运动垫',3,'跃动',55.00,139.00,'2024-02-20',1),
  (7,'G007','跑步鞋','运动鞋,慢跑鞋',3,'飞驰',190.00,459.00,'2024-01-10',1),
  (8,'G008','商务双肩包','电脑包,通勤包',4,'远行',110.00,269.00,'2024-04-01',1),
  (9,'G009','真皮钱包','钱夹,皮夹',4,'匠心',80.00,199.00,'2024-03-15',1),
  (10,'G010','坚果礼盒','零食礼盒,混合坚果',5,'食悦',65.00,159.00,'2024-01-01',1),
  (11,'G011','精品咖啡豆','咖啡,咖啡豆',5,'醇享',58.00,129.00,'2024-05-01',1),
  (12,'G012','显示器支架','屏幕支架,桌面支架',1,'极客',95.00,229.00,'2024-06-01',1);

INSERT INTO promotion_activity VALUES
  (1,'春节焕新季','节日促销',0.8500,'2025-01-20 00:00:00','2025-02-12 23:59:59',80000,1),
  (2,'618年中大促','平台大促',0.7800,'2025-06-01 00:00:00','2025-06-20 23:59:59',150000,1),
  (3,'双十一购物节','平台大促',0.7200,'2025-11-01 00:00:00','2025-11-15 23:59:59',240000,1),
  (4,'春节焕新季2026','节日促销',0.8300,'2026-01-20 00:00:00','2026-02-20 23:59:59',100000,1),
  (5,'618年中大促2026','平台大促',0.7500,'2026-06-01 00:00:00','2026-06-20 23:59:59',180000,1);

-- 使用存储过程生成2025-01-01至2026-08-31的稳定模拟数据。
-- 常规每天4单；大促期间8单；2026年3月人为制造销售下降；
-- 2026年6月制造促销高峰；2026年7月制造退款率升高，供异常/归因测试。
DROP PROCEDURE IF EXISTS seed_ecommerce_orders;
DELIMITER $$
CREATE PROCEDURE seed_ecommerce_orders()
BEGIN
  DECLARE v_date DATE DEFAULT '2025-01-01';
  DECLARE v_day_orders INT;
  DECLARE v_i INT;
  DECLARE v_order_id BIGINT DEFAULT 100000;
  DECLARE v_item_id BIGINT DEFAULT 200000;
  DECLARE v_payment_id BIGINT DEFAULT 300000;
  DECLARE v_refund_id BIGINT DEFAULT 400000;
  DECLARE v_customer BIGINT;
  DECLARE v_shop BIGINT;
  DECLARE v_goods BIGINT;
  DECLARE v_qty INT;
  DECLARE v_price DECIMAL(12,2);
  DECLARE v_cost DECIMAL(12,2);
  DECLARE v_discount_rate DECIMAL(6,4);
  DECLARE v_activity BIGINT;
  DECLARE v_goods_amount DECIMAL(14,2);
  DECLARE v_discount DECIMAL(14,2);
  DECLARE v_pay DECIMAL(14,2);
  DECLARE v_cost_amount DECIMAL(14,2);
  DECLARE v_channel VARCHAR(20);
  DECLARE v_method VARCHAR(20);
  DECLARE v_status VARCHAR(20);

  START TRANSACTION;
  WHILE v_date <= '2026-08-31' DO
    SET v_day_orders = 4;
    IF (v_date BETWEEN '2025-06-01' AND '2025-06-20')
       OR (v_date BETWEEN '2025-11-01' AND '2025-11-15')
       OR (v_date BETWEEN '2026-06-01' AND '2026-06-20') THEN
      SET v_day_orders = 8;
    END IF;
    IF v_date BETWEEN '2026-03-01' AND '2026-03-31' THEN
      SET v_day_orders = 2;
    END IF;

    SET v_i = 1;
    WHILE v_i <= v_day_orders DO
      SET v_order_id = v_order_id + 1;
      SET v_item_id = v_item_id + 1;
      SET v_payment_id = v_payment_id + 1;
      SET v_customer = MOD(v_order_id * 7, 12) + 1;
      SET v_shop = MOD(v_order_id * 5, 4) + 1;
      SET v_goods = MOD(v_order_id * 11 + DAY(v_date), 12) + 1;
      SET v_qty = MOD(v_order_id, 3) + 1;
      SET v_channel = ELT(MOD(v_order_id,4)+1,'APP','小程序','网页','直播');
      SET v_method = ELT(MOD(v_order_id,3)+1,'WECHAT','ALIPAY','CARD');
      SET v_status = IF(MOD(v_order_id,29)=0,'CANCELLED','COMPLETED');

      SELECT sale_price, cost_price INTO v_price, v_cost
        FROM goods_info WHERE goods_id = v_goods;

      SET v_activity = NULL;
      SET v_discount_rate = 1.0000;
      IF v_date BETWEEN '2025-01-20' AND '2025-02-12' THEN SET v_activity=1; SET v_discount_rate=0.8500;
      ELSEIF v_date BETWEEN '2025-06-01' AND '2025-06-20' THEN SET v_activity=2; SET v_discount_rate=0.7800;
      ELSEIF v_date BETWEEN '2025-11-01' AND '2025-11-15' THEN SET v_activity=3; SET v_discount_rate=0.7200;
      ELSEIF v_date BETWEEN '2026-01-20' AND '2026-02-20' THEN SET v_activity=4; SET v_discount_rate=0.8300;
      ELSEIF v_date BETWEEN '2026-06-01' AND '2026-06-20' THEN SET v_activity=5; SET v_discount_rate=0.7500;
      END IF;

      SET v_goods_amount = v_price * v_qty;
      SET v_discount = ROUND(v_goods_amount * (1-v_discount_rate),2);
      SET v_pay = IF(v_status='CANCELLED',0,ROUND(v_goods_amount-v_discount+IF(v_goods_amount<199,10,0),2));
      SET v_cost_amount = v_cost * v_qty;

      INSERT INTO order_info VALUES (
        v_order_id, CONCAT('EC',DATE_FORMAT(v_date,'%Y%m%d'),LPAD(v_i,3,'0')),
        v_customer,v_shop,v_activity,v_channel,v_status,
        TIMESTAMP(v_date,MAKETIME(9+MOD(v_i,12),MOD(v_order_id,60),0)),
        IF(v_status='CANCELLED',NULL,TIMESTAMP(v_date,MAKETIME(9+MOD(v_i,12),MOD(v_order_id,60),30))),
        v_goods_amount,v_discount,IF(v_goods_amount<199,10,0),v_pay,v_cost_amount,
        ELT(v_shop,'上海','北京','广东','四川'),ELT(v_shop,'上海','北京','深圳','成都'),NOW()
      );

      INSERT INTO order_item_detail VALUES
        (v_item_id,v_order_id,v_goods,v_qty,v_price,v_goods_amount,v_discount,
         GREATEST(v_pay-IF(v_goods_amount<199,10,0),0),v_cost_amount);

      IF v_status <> 'CANCELLED' THEN
        INSERT INTO payment_record VALUES
          (v_payment_id,CONCAT('PAY',v_order_id),v_order_id,v_method,'SUCCESS',v_pay,
           TIMESTAMP(v_date,MAKETIME(9+MOD(v_i,12),MOD(v_order_id,60),30)));
      END IF;

      -- 常规约3.4%退款；2026年7月约20%退款，制造可解释异常。
      IF v_status <> 'CANCELLED'
         AND (MOD(v_order_id,29)=1 OR (v_date BETWEEN '2026-07-01' AND '2026-07-31' AND MOD(v_order_id,5)=0)) THEN
        SET v_refund_id = v_refund_id + 1;
        INSERT INTO refund_record VALUES (
          v_refund_id,CONCAT('RF',v_order_id),v_order_id,v_goods,'全额退款',
          IF(v_date BETWEEN '2026-07-01' AND '2026-07-31','物流延迟','商品不符合预期'),
          GREATEST(v_pay-IF(v_goods_amount<199,10,0),0),
          DATE_ADD(TIMESTAMP(v_date,'12:00:00'),INTERVAL 2 DAY),
          DATE_ADD(TIMESTAMP(v_date,'15:00:00'),INTERVAL 3 DAY),'COMPLETED'
        );
        UPDATE order_info SET order_status='REFUNDED' WHERE order_id=v_order_id;
      END IF;

      SET v_i = v_i + 1;
    END WHILE;
    SET v_date = DATE_ADD(v_date, INTERVAL 1 DAY);
  END WHILE;
  COMMIT;
END$$
DELIMITER ;

CALL seed_ecommerce_orders();
DROP PROCEDURE IF EXISTS seed_ecommerce_orders;

-- 生成每月末库存快照
INSERT INTO inventory_snapshot
  (snapshot_date,shop_id,goods_id,available_quantity,locked_quantity,safety_stock,inventory_amount)
SELECT month_end, s.shop_id, g.goods_id,
       20 + MOD(MONTH(month_end)*7 + s.shop_id*3 + g.goods_id*5,80) AS available_quantity,
       MOD(g.goods_id+s.shop_id,5) AS locked_quantity,
       15 AS safety_stock,
       ROUND((20 + MOD(MONTH(month_end)*7+s.shop_id*3+g.goods_id*5,80))*g.cost_price,2)
FROM (
  SELECT LAST_DAY('2025-01-01') month_end UNION ALL SELECT LAST_DAY('2025-02-01')
  UNION ALL SELECT LAST_DAY('2025-03-01') UNION ALL SELECT LAST_DAY('2025-04-01')
  UNION ALL SELECT LAST_DAY('2025-05-01') UNION ALL SELECT LAST_DAY('2025-06-01')
  UNION ALL SELECT LAST_DAY('2025-07-01') UNION ALL SELECT LAST_DAY('2025-08-01')
  UNION ALL SELECT LAST_DAY('2025-09-01') UNION ALL SELECT LAST_DAY('2025-10-01')
  UNION ALL SELECT LAST_DAY('2025-11-01') UNION ALL SELECT LAST_DAY('2025-12-01')
  UNION ALL SELECT LAST_DAY('2026-01-01') UNION ALL SELECT LAST_DAY('2026-02-01')
  UNION ALL SELECT LAST_DAY('2026-03-01') UNION ALL SELECT LAST_DAY('2026-04-01')
  UNION ALL SELECT LAST_DAY('2026-05-01') UNION ALL SELECT LAST_DAY('2026-06-01')
  UNION ALL SELECT LAST_DAY('2026-07-01') UNION ALL SELECT LAST_DAY('2026-08-01')
) months CROSS JOIN shop_info s CROSS JOIN goods_info g;

-- 宽表视图：便于明细查询和智能体联调
CREATE VIEW v_order_wide AS
SELECT o.order_id,o.order_no,o.order_time,o.pay_time,o.order_status,o.order_channel,
       o.total_goods_amount,o.discount_amount,o.freight_amount,o.pay_amount,o.cost_amount,
       ROUND(o.pay_amount-o.cost_amount,2) AS gross_profit_amount,
       c.customer_id,c.customer_name,c.gender,c.member_level,c.register_date,
       s.shop_id,s.shop_name,s.region_code,s.region_name,s.city_name,
       i.goods_id,g.goods_code,g.goods_name,g.brand_name,cat.category_code,cat.category_name,
       i.quantity,i.unit_price,i.actual_amount,o.activity_id,a.activity_name,
       CASE WHEN r.refund_id IS NULL THEN 0 ELSE 1 END AS is_refunded,
       COALESCE(r.refund_amount,0) AS refund_amount,r.refund_reason
FROM order_info o
JOIN customer_info c ON c.customer_id=o.customer_id
JOIN shop_info s ON s.shop_id=o.shop_id
JOIN order_item_detail i ON i.order_id=o.order_id
JOIN goods_info g ON g.goods_id=i.goods_id
JOIN goods_category cat ON cat.category_id=g.category_id
LEFT JOIN promotion_activity a ON a.activity_id=o.activity_id
LEFT JOIN refund_record r ON r.order_id=o.order_id AND r.refund_status='COMPLETED';

CREATE VIEW v_daily_sales AS
SELECT DATE(pay_time) AS statistical_date,
       COUNT(DISTINCT order_id) AS paid_order_count,
       COUNT(DISTINCT customer_id) AS paying_customer_count,
       ROUND(SUM(pay_amount),2) AS sales_amount,
       ROUND(SUM(cost_amount),2) AS cost_amount,
       ROUND(SUM(pay_amount-cost_amount),2) AS gross_profit_amount,
       ROUND(SUM(pay_amount)/NULLIF(COUNT(DISTINCT order_id),0),2) AS avg_order_value
FROM order_info
WHERE pay_time IS NOT NULL
GROUP BY DATE(pay_time);

CREATE VIEW v_monthly_sales AS
SELECT DATE_FORMAT(pay_time,'%Y-%m') AS statistical_month,
       COUNT(DISTINCT o.order_id) AS paid_order_count,
       COUNT(DISTINCT o.customer_id) AS paying_customer_count,
       ROUND(SUM(o.pay_amount),2) AS sales_amount,
       ROUND(SUM(o.cost_amount),2) AS cost_amount,
       ROUND(SUM(o.pay_amount-o.cost_amount),2) AS gross_profit_amount,
       ROUND(SUM(o.pay_amount)/NULLIF(COUNT(DISTINCT o.order_id),0),2) AS avg_order_value,
       ROUND(SUM(COALESCE(r.refund_amount,0)),2) AS refund_amount,
       ROUND(SUM(COALESCE(r.refund_amount,0))/NULLIF(SUM(o.pay_amount),0),4) AS refund_rate
FROM order_info o
LEFT JOIN refund_record r ON r.order_id=o.order_id AND r.refund_status='COMPLETED'
WHERE pay_time IS NOT NULL
GROUP BY DATE_FORMAT(pay_time,'%Y-%m');

-- 数据质量和规模检查
SELECT 'shop_info' table_name, COUNT(*) row_count FROM shop_info
UNION ALL SELECT 'customer_info',COUNT(*) FROM customer_info
UNION ALL SELECT 'goods_info',COUNT(*) FROM goods_info
UNION ALL SELECT 'order_info',COUNT(*) FROM order_info
UNION ALL SELECT 'order_item_detail',COUNT(*) FROM order_item_detail
UNION ALL SELECT 'payment_record',COUNT(*) FROM payment_record
UNION ALL SELECT 'refund_record',COUNT(*) FROM refund_record
UNION ALL SELECT 'inventory_snapshot',COUNT(*) FROM inventory_snapshot;

-- 建议测试问题：
-- 1. 查询2026年7月销售额、订单量和客单价。
-- 2. 分析2025年1月至2026年8月销售额趋势。
-- 3. 对比2025年和2026年6月销售额，计算同比增长率。
-- 4. 分析2026年销售额按地区、渠道和商品品类的占比。
-- 5. 识别2026年1月至8月销售额及退款率异常月份。
-- 6. 分析2026年3月销售额下降的主要原因。
-- 7. 分析2026年7月退款率上升的原因。
-- 8. 根据历史月度销售额预测2026年9月销售额。
-- 9. 查询无线蓝牙耳机在各地区、渠道的销量和销售额。
-- 10. 生成2026年上半年经营分析报告。
