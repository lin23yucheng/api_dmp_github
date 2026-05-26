"""
项目管理数据库查询仓储层。
"""
from common.db_client import DbClient


class ProjectManagementRepository:
    def __init__(self, db_client: DbClient):
        self.db_client = db_client

    def get_active_industries(self):
        sql = """
        SELECT industry_id, industry_name
        FROM industry_dictionary
        WHERE is_delete = FALSE
        ORDER BY industry_id ASC
        """
        rows = self.db_client.query_all(sql)
        return [
            {
                "industry_id": int(row[0]),
                "industry_name": str(row[1]),
            }
            for row in rows
        ]

    def get_industry_dictionary_count(self):
        sql = """
        SELECT COUNT(1)
        FROM industry_dictionary
        WHERE is_delete = FALSE
        """
        row = self.db_client.query_one(sql)
        return int(row[0]) if row else 0

    def pick_industry(self, exclude_industry_id=None):
        industries = self.get_active_industries()
        if not industries:
            raise AssertionError("industry_dictionary表无可用行业数据")

        for item in industries:
            if exclude_industry_id is None or int(item["industry_id"]) != int(exclude_industry_id):
                return item

        return industries[0]

