-- 供应商-科室匹配度 TopN 参考SQL（MySQL 8+）
-- 表名仅作为语义层/数据侧建模示例，不由智能体直接拼接执行。
-- 必须使用参数绑定：:hospital_id、:product_id、:start_time、:end_time、:top_n。

WITH eligible_departments AS (
    SELECT d.department_id
    FROM hospital_department d
    WHERE d.hospital_id = :hospital_id
      AND d.is_active = 1
      AND d.is_test = 0
),
eligible_suppliers AS (
    SELECT DISTINCT sp.supplier_id
    FROM supplier_product sp
    WHERE sp.product_id = :product_id
      AND sp.is_active = 1
),
matched_departments AS (
    SELECT
        p.supplier_id,
        COUNT(DISTINCT p.department_id) AS matched_department_count
    FROM department_purchase p
    INNER JOIN eligible_departments d
        ON d.department_id = p.department_id
    WHERE p.hospital_id = :hospital_id
      AND p.product_id = :product_id
      AND p.purchase_time >= :start_time
      AND p.purchase_time < :end_time
      AND p.purchase_status = 'VALID'
    GROUP BY p.supplier_id
),
department_total AS (
    SELECT COUNT(*) AS eligible_department_count
    FROM eligible_departments
)
SELECT
    s.supplier_id AS supplier_id,
    s.supplier_name AS `供应商`,
    COALESCE(m.matched_department_count, 0) AS `匹配科室数`,
    t.eligible_department_count AS `有效科室总数`,
    ROUND(
        COALESCE(m.matched_department_count, 0)
        / NULLIF(t.eligible_department_count, 0) * 100,
        2
    ) AS `科室匹配度`
FROM eligible_suppliers es
INNER JOIN supplier s
    ON s.supplier_id = es.supplier_id
LEFT JOIN matched_departments m
    ON m.supplier_id = es.supplier_id
CROSS JOIN department_total t
WHERE s.is_active = 1
  AND t.eligible_department_count > 0
ORDER BY `科室匹配度` DESC, s.supplier_id ASC
LIMIT :top_n;
