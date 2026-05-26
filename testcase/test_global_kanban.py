"""
全局看板
"""
import json

import pytest
import allure

from api import api_login, api_space_management, api_machine_management
from common.Request_Response import ApiClient
from common import Assert
from common.db_client import DbClient
from repository.kanban_repo import KanbanRepository

assertions = Assert.Assertions()

# 初始化全局客户端
base_headers = {
    "Authorization": api_login.ApiLogin().login(),
}
global_client = ApiClient(base_headers=base_headers)


@allure.feature("场景：全局看板流程")
class TestGlobalKanban:
    @classmethod
    def setup_class(cls):
        """初始化接口封装实例。"""
        cls.api_space_manage = api_space_management.ApiSpaceManage(global_client)
        cls.api_machine = api_machine_management.ApiMachine(global_client)
        cls.kanban_repo = KanbanRepository(DbClient())

    @staticmethod
    def _attach_json(data, name):
        allure.attach(json.dumps(data, ensure_ascii=False, indent=2), name=name, attachment_type=allure.attachment_type.JSON)

    @staticmethod
    def _attach_response(response, step_name):
        """将响应内容附加到Allure，便于排查。"""
        try:
            body = response.json()
        except Exception:
            body = {"raw_text": response.text}
        TestGlobalKanban._attach_json(body, f"{step_name}响应数据")
        return body

    def _assert_common_response(self, response, step_name):
        """通用断言：校验HTTP状态码与业务success字段。"""
        assertions.assert_code(response.status_code, 200)
        body = self._attach_response(response, step_name)

        assertions.assert_is_not_none(body.get("success"), f"{step_name}响应缺少 success 字段")
        assert body.get("success") is True, f"{step_name}业务失败: {body}"
        return body

    def _assert_db_match_for_type_statistics(self, response_body, step_name, space_status=None):
        """步骤1/2/3：对比接口统计与数据库统计结果。"""
        api_data = response_body.get("data") or {}
        db_data = self.kanban_repo.get_space_type_statistics(space_status=space_status)

        compare_result = {
            "space_status": space_status,
            "api": {
                "inspection": int(api_data.get("inspection", 0) or 0),
                "operation": int(api_data.get("operation", 0) or 0),
                "polish": int(api_data.get("polish", 0) or 0),
                "welding": int(api_data.get("welding", 0) or 0),
            },
            "db": db_data,
        }
        self._attach_json(compare_result, f"{step_name}接口与数据库对比")

        assert compare_result["api"] == compare_result["db"], f"{step_name}统计不一致: {compare_result}"

    def _assert_db_match_for_monthly_trend(self, response_body, step_name, space_type=None):
        """步骤4：对比近12个月趋势接口与数据库结果。"""
        api_list = response_body.get("data") or []
        db_list = self.kanban_repo.get_monthly_trend_statistics(space_type=space_type)

        normalized_api = [
            {
                "month": str(item.get("month", "")),
                "startCount": int(item.get("startCount", 0) or 0),
                "finishedCount": int(item.get("finishedCount", 0) or 0),
            }
            for item in api_list
        ]

        compare_result = {"api": normalized_api, "db": db_list}
        self._attach_json(compare_result, f"{step_name}接口与数据库对比")

        assert normalized_api == db_list, f"{step_name}趋势不一致"

    def _assert_db_match_for_industry_distribution(self, response_body, step_name):
        """步骤5：对比行业分布接口与数据库结果（前五 + Others）。"""
        api_list = response_body.get("data") or []
        db_list = self.kanban_repo.get_industry_distribution_statistics()

        normalized_api = [
            {"key": str(item.get("key", "")), "value": int(item.get("value", 0) or 0)}
            for item in api_list
        ]

        compare_result = {"api": normalized_api, "db": db_list}
        self._attach_json(compare_result, f"{step_name}接口与数据库对比")

        assert normalized_api == db_list, f"{step_name}行业分布不一致"

    def _assert_db_match_for_recent_projects(self, response_body, step_name):
        """步骤6：对比最近5个项目（按创建时间倒序）。"""
        api_list = response_body.get("data") or []
        db_rows = self.kanban_repo.get_recent_projects(limit_count=5)

        field_map = {
            "spaceManageId": "space_manage_id",
            "spaceName": "space_name",
            "spaceStatus": "space_status",
            "spaceType": "space_type",
            "productNum": "product_num",
        }

        def query_device_num_by_space_id(space_manage_id):
            machine_resp = self.api_machine.page(spaceId=str(space_manage_id), status=1, pageIndex=1, pageSize=9999)
            assertions.assert_code(machine_resp.status_code, 200)
            machine_body = machine_resp.json()
            assertions.assert_is_not_none(machine_body.get("success"), "设备分页响应缺少 success 字段")
            assert machine_body.get("success") is True, f"设备分页业务失败: {machine_body}"

            data = machine_body.get("data") or {}
            total = data.get("total")
            if total is not None:
                return int(total or 0)
            return len(data.get("list") or [])

        assert len(api_list) == len(db_rows), f"{step_name}项目数量不一致: api={len(api_list)}, db={len(db_rows)}"

        compare_view = []
        for idx, (api_item, db_item) in enumerate(zip(api_list, db_rows), start=1):
            row_compare = {"index": idx, "fields": {}}

            for api_key, db_key in field_map.items():
                api_norm = str(api_item.get(api_key)) if api_item.get(api_key) is not None else None
                db_norm = str(db_item.get(db_key)) if db_item.get(db_key) is not None else None
                row_compare["fields"][api_key] = {"api": api_norm, "db": db_norm}
                assert api_norm == db_norm, f"{step_name}第{idx}条 {api_key}不一致: api={api_norm}, db={db_norm}"

            space_manage_id = api_item.get("spaceManageId")
            api_device_num = int(api_item.get("deviceNum", 0) or 0)
            machine_device_num = query_device_num_by_space_id(space_manage_id)
            row_compare["fields"]["deviceNum"] = {"api": api_device_num, "machine_api": machine_device_num}
            assert api_device_num == machine_device_num, (
                f"{step_name}第{idx}条 deviceNum不一致: "
                f"api={api_device_num}, machine_api={machine_device_num}, spaceManageId={space_manage_id}"
            )

            compare_view.append(row_compare)

        self._attach_json(compare_view, f"{step_name}接口与数据库对比")

    @allure.story("全局看板")
    @allure.title("全局看板流程：项目统计与趋势查询")
    def test_global_kanban_flow(self):
        with allure.step("步骤1：查询进行中的项目类型统计"):
            response = self.api_space_manage.statistics_type(spaceStatus=1)
            response_body = self._assert_common_response(response, "进行中的项目类型统计")
            self._assert_db_match_for_type_statistics(response_body, "进行中的项目类型统计", space_status=1)

        with allure.step("步骤2：查询已结项的项目类型统计"):
            response = self.api_space_manage.statistics_type(spaceStatus=2)
            response_body = self._assert_common_response(response, "已结项的项目类型统计")
            self._assert_db_match_for_type_statistics(response_body, "已结项的项目类型统计", space_status=2)

        with allure.step("步骤3：查询全部的项目类型统计"):
            response = self.api_space_manage.statistics_type(spaceStatus=None)
            response_body = self._assert_common_response(response, "全部的项目类型统计")
            self._assert_db_match_for_type_statistics(response_body, "全部的项目类型统计", space_status=None)

        with allure.step("步骤4：查询近12个月项目开展趋势"):
            response = self.api_space_manage.statistics_time(spaceType="")
            response_body = self._assert_common_response(response, "近12个月项目开展趋势")
            self._assert_db_match_for_monthly_trend(response_body, "近12个月项目开展趋势", space_type=None)

        with allure.step("步骤5：查询行业分布"):
            response = self.api_space_manage.statistics_industry()
            response_body = self._assert_common_response(response, "行业分布")
            self._assert_db_match_for_industry_distribution(response_body, "行业分布")

        with allure.step("步骤6：查询最近项目"):
            response = self.api_space_manage.statistics_active()
            response_body = self._assert_common_response(response, "最近项目")
            self._assert_db_match_for_recent_projects(response_body, "最近项目")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--alluredir=./allure-results"])
