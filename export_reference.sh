#!/usr/bin/env bash
# Выгрузка справочника из BigQuery в reference.csv. Запускать в Cloud Shell (там уже есть bq и доступ).
#   bash export_reference.sh
# Файл потом скачать (Cloud Shell → ⋮ → Download) и положить рядом с app.py.
# Если запускаете на сервере, где настроен доступ к BigQuery, можно вместо файла поставить REFERENCE_SOURCE=bq.
set -euo pipefail
PROJECT="${BQ_PROJECT:-reorder-497714}"

bq query --use_legacy_sql=false --project_id="$PROJECT" --format=csv --max_rows=200000 "
SELECT
  p.asin, p.old_asin, p.group_key, p.parent_group, p.category, p.color, p.size,
  p.abcd_class, p.active_us,
  pp.plan_units AS plan_units_month
FROM \`$PROJECT.forecast.dim_product\` p
LEFT JOIN (
  SELECT asin, SUM(plan_units) AS plan_units
  FROM \`$PROJECT.forecast.psi_projection\`
  WHERE month = DATE_TRUNC(CURRENT_DATE(), MONTH)
  GROUP BY asin
) pp USING (asin)
" > reference.csv

echo "Готово: reference.csv ($(($(wc -l < reference.csv) - 1)) строк)"
