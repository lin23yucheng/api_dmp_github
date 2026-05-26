"""
全局看板数据库查询仓储层。
"""
from common.db_client import DbClient


class KanbanRepository:
    def __init__(self, db_client: DbClient):
        self.db_client = db_client

    def get_space_type_statistics(self, space_status=None):
        sql = """
        SELECT
            COUNT(*) FILTER (WHERE space_type = 1) AS inspection,
            COUNT(*) FILTER (WHERE space_type = 3) AS operation,
            COUNT(*) FILTER (WHERE space_type = 4) AS polish,
            COUNT(*) FILTER (WHERE space_type = 5) AS welding
        FROM space_manage
        WHERE is_delete = FALSE
        """
        params = []
        if space_status in (1, 2):
            sql += " AND space_status = %s"
            params.append(space_status)

        row = self.db_client.query_one(sql, params=params)
        return {
            "inspection": int(row[0] or 0),
            "operation": int(row[1] or 0),
            "polish": int(row[2] or 0),
            "welding": int(row[3] or 0),
        }

    def get_monthly_trend_statistics(self, space_type=None):
        sql = """
        WITH months AS (
            SELECT generate_series(
                date_trunc('month', CURRENT_DATE) - interval '11 month',
                date_trunc('month', CURRENT_DATE),
                interval '1 month'
            ) AS month_start
        ),
        start_stats AS (
            SELECT date_trunc('month', create_time) AS month_start, COUNT(*) AS start_count
            FROM space_manage
            WHERE is_delete = FALSE
              AND create_time >= date_trunc('month', CURRENT_DATE) - interval '11 month'
              AND create_time < date_trunc('month', CURRENT_DATE) + interval '1 month'
              AND (%s IS NULL OR space_type = %s)
            GROUP BY 1
        ),
        finished_stats AS (
            SELECT date_trunc('month', finished_time) AS month_start, COUNT(*) AS finished_count
            FROM space_manage
            WHERE is_delete = FALSE
              AND finished_time IS NOT NULL
              AND finished_time >= date_trunc('month', CURRENT_DATE) - interval '11 month'
              AND finished_time < date_trunc('month', CURRENT_DATE) + interval '1 month'
              AND (%s IS NULL OR space_type = %s)
            GROUP BY 1
        )
        SELECT
            to_char(m.month_start, 'YYYY-MM') AS month,
            COALESCE(s.start_count, 0) AS "startCount",
            COALESCE(f.finished_count, 0) AS "finishedCount"
        FROM months m
        LEFT JOIN start_stats s ON s.month_start = m.month_start
        LEFT JOIN finished_stats f ON f.month_start = m.month_start
        ORDER BY m.month_start
        """

        rows = self.db_client.query_all(sql, params=[space_type, space_type, space_type, space_type])
        return [
            {
                "month": row[0],
                "startCount": int(row[1] or 0),
                "finishedCount": int(row[2] or 0),
            }
            for row in rows
        ]

    def get_industry_distribution_statistics(self):
        sql = """
        SELECT industry_name, COUNT(*) AS cnt
        FROM space_manage
        WHERE is_delete = FALSE
          AND industry_name IS NOT NULL
          AND btrim(industry_name) <> ''
        GROUP BY industry_name
        ORDER BY cnt DESC, industry_name ASC
        """
        rows = self.db_client.query_all(sql)

        all_items = [{"key": str(row[0]), "value": int(row[1] or 0)} for row in rows]
        top_five = all_items[:5]
        others_sum = sum(int(item["value"]) for item in all_items[5:])
        if others_sum > 0:
            top_five.append({"key": "Others", "value": int(others_sum)})
        return top_five

    def get_recent_projects(self, limit_count=5):
        sql = """
        SELECT
            space_manage_id,
            space_name,
            space_status,
            space_type,
            product_num,
            device_num,
            industry_name,
            finished_time,
            create_time
        FROM space_manage
        WHERE is_delete = FALSE
        ORDER BY create_time DESC
        LIMIT %s
        """
        return self.db_client.query_all_dict(sql, params=[limit_count])
