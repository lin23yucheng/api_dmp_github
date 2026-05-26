"""
项目管理
"""
import json
from datetime import datetime

import allure
import pytest

from api import api_login, api_space_management, api_user_info, api_machine_management, api_product_info, api_industry_dictionary
from common import Assert
from common.Request_Response import ApiClient
from common.db_client import DbClient
from repository.project_management_repo import ProjectManagementRepository

assertions = Assert.Assertions()

base_headers = {
    "Authorization": api_login.ApiLogin().login(),
}
global_client = ApiClient(base_headers=base_headers)


@allure.feature("场景：项目管理流程")
class TestProjectManagement:
    @classmethod
    def setup_class(cls):
        cls.api_space_manage = api_space_management.ApiSpaceManage(global_client)
        cls.api_user_info = api_user_info.ApiUserInfo(global_client)
        cls.api_machine = api_machine_management.ApiMachine(global_client)
        cls.api_product = api_product_info.ApiProductInfo(global_client)
        cls.api_industry_dict = api_industry_dictionary.ApiIndustryDictionary(global_client)
        cls.project_repo = ProjectManagementRepository(DbClient())
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        cls.flow_context = {
            "timestamp": timestamp,
            "fixed_space_name": "Test_zdh",
            "update_space_explain": f"修改描述_{timestamp}",
            "product_code": f"test_{timestamp}",
            "product_show_name": f"接口自动化_{timestamp}",
            "updated_product_show_name": f"修改接口自动化_{timestamp}",
        }

    @staticmethod
    def _attach_json(data, name):
        allure.attach(json.dumps(data, ensure_ascii=False, indent=2), name=name, attachment_type=allure.attachment_type.JSON)

    def _assert_common_response(self, response, step_name):
        assertions.assert_code(response.status_code, 200)
        body = response.json()
        self._attach_json(body, f"{step_name}响应")
        assertions.assert_is_not_none(body.get("success"), f"{step_name}响应缺少 success 字段")
        assert body.get("success") is True, f"{step_name}失败: {body}"
        return body

    def _assert_status_and_msg_success(self, response, step_name):
        assertions.assert_code(response.status_code, 200)
        body = response.json()
        self._attach_json(body, f"{step_name}响应")
        msg = str(body.get("msg") or "")
        assert "成功" in msg, f"{step_name}响应msg未包含'成功': {msg}, body={body}"
        return body

    @staticmethod
    def _find_target_space(space_list, space_name):
        for item in space_list:
            if str(item.get("spaceName")) == str(space_name):
                return item
        return None

    def _resolve_fixed_space_manage_id(self):
        ctx = self.__class__.flow_context
        existing_space_manage_id = ctx.get("space_manage_id")
        if existing_space_manage_id is not None:
            return existing_space_manage_id

        fixed_space_name = ctx.get("fixed_space_name", "Test_zdh")
        response = self.api_space_manage.page(
            spaceName=fixed_space_name,
            spaceStatus="",
            spaceType="",
            pageIndex=1,
            pageSize=20,
        )
        response_body = self._assert_common_response(response, "查询固定空间(总览前置)")
        data = response_body.get("data") or {}
        space_list = data.get("list") or []
        target_space = self._find_target_space(space_list, fixed_space_name)
        assert target_space is not None, f"未查询到固定空间: {fixed_space_name}"

        extracted_space_manage_id = target_space.get("spaceManageId")
        assertions.assert_is_not_none(extracted_space_manage_id, "固定空间缺少 spaceManageId")
        ctx["space_manage_id"] = extracted_space_manage_id
        self._attach_json(
            {
                "fixedSpaceName": fixed_space_name,
                "spaceManageId": extracted_space_manage_id,
            },
            "总览流程提取固定空间ID"
        )
        return extracted_space_manage_id

    @allure.story("项目管理")
    @pytest.mark.order(1)
    @allure.title("项目管理流程-1：空间生命周期")
    def test_space_lifecycle(self):
        self._run_space_lifecycle()

    @allure.story("项目管理")
    @pytest.mark.order(2)
    @allure.title("项目管理流程-2：成员与机器管理")
    def test_member_machine_flow(self):
        self._run_member_machine_flow()

    @allure.story("项目管理")
    @pytest.mark.order(3)
    @allure.title("项目管理流程-3：产品管理")
    def test_product_flow(self):
        self._run_product_flow()

    @allure.story("项目管理")
    @pytest.mark.order(4)
    @allure.title("项目管理流程-4：项目总览")
    def test_project_overview_flow(self):
        self._run_project_overview_flow()

    def _run_space_lifecycle(self):
        ctx = self.__class__.flow_context
        fixed_space_name = ctx["fixed_space_name"]
        update_space_explain = ctx["update_space_explain"]

        with allure.step("步骤1：新增项目空间"):
            industry_resp = self.api_industry_dict.select_all()
            industry_body = self._assert_common_response(industry_resp, "获取行业字典(新增项目空间前置)")
            industry_list = industry_body.get("data") or []
            assert industry_list, "行业字典为空，无法执行新增项目空间"

            industry_id_for_add = industry_list[0].get("industryId")
            assertions.assert_is_not_none(industry_id_for_add, "行业字典首条缺少 industryId")

            add_payload = {
                "spaceName": "Test_zdh",
                "spaceCnName": "自动化",
                "spaceType": 1,
                "industryId": industry_id_for_add,
                "spaceExplain": "自动化",
            }
            self._attach_json(add_payload, "新增项目空间请求参数")

            add_resp = self.api_space_manage.add(
                spaceName="Test_zdh",
                spaceCnName="自动化",
                spaceType=1,
                industryId=industry_id_for_add,
                spaceExplain="自动化",
            )
            assertions.assert_code(add_resp.status_code, 200)
            add_body = add_resp.json()
            self._attach_json(add_body, "新增项目空间响应")
            assert str(add_body.get("msg") or "") == "项目标识已存在", (
                f"新增项目空间预期失败文案不匹配: {add_body}"
            )

        with allure.step("步骤2：查询固定空间Test_zdh并获取空间信息"):
            response = self.api_space_manage.page(
                spaceName=fixed_space_name,
                spaceStatus="",
                spaceType="",
                pageIndex=1,
                pageSize=20,
            )
            response_body = self._assert_common_response(response, "查询固定空间Test_zdh")

            data = response_body.get("data") or {}
            space_list = data.get("list") or []
            target_space = self._find_target_space(space_list, fixed_space_name)
            assert target_space is not None, f"未查询到固定空间: {fixed_space_name}"

            space_manage_id = target_space.get("spaceManageId")
            space_cn_name = target_space.get("spaceCnName")
            space_status = target_space.get("spaceStatus")
            space_type_value = target_space.get("spaceType")
            current_industry_id = target_space.get("industryId")

            assertions.assert_is_not_none(space_manage_id, "固定空间缺少 spaceManageId")
            assertions.assert_is_not_none(space_cn_name, "固定空间缺少 spaceCnName")
            assertions.assert_is_not_none(space_status, "固定空间缺少 spaceStatus")

            self._attach_json(
                {
                    "spaceManageId": space_manage_id,
                    "spaceCnName": space_cn_name,
                    "spaceExplain": target_space.get("spaceExplain"),
                    "spaceStatus": space_status,
                    "spaceType": space_type_value,
                    "industryId": current_industry_id,
                },
                "固定空间查询数据",
            )

            if int(space_status) != 1:
                restart_resp = self.api_space_manage.restart(spaceManageId=space_manage_id)
                self._assert_common_response(restart_resp, "前置重启固定空间")

        with allure.step("步骤3：获取行业字典并校验行业字典数量"):
            industry_resp = self.api_industry_dict.select_all()
            industry_body = self._assert_common_response(industry_resp, "获取行业字段")

            industry_list = industry_body.get("data") or []
            api_count = len(industry_list)
            db_count = self.project_repo.get_industry_dictionary_count()
            self._attach_json(
                {
                    "industryApiCount": api_count,
                    "industryDbCount": db_count,
                },
                "行业字典数量对比"
            )
            assert api_count == db_count, f"行业字典数量不一致: api={api_count}, db={db_count}"

        with allure.step("步骤4：编辑空间"):
            updated_industry = self.project_repo.pick_industry(exclude_industry_id=current_industry_id)
            update_payload = {
                "spaceName": fixed_space_name,
                "spaceCnName": space_cn_name,
                "spaceType": space_type_value,
                "industryId": updated_industry["industry_id"],
                "spaceExplain": update_space_explain,
                "spaceId": space_manage_id,
            }
            self._attach_json(update_payload, "编辑空间请求参数")

            response = self.api_space_manage.update_space(
                spaceName=fixed_space_name,
                spaceCnName=space_cn_name,
                spaceType=space_type_value,
                industryId=updated_industry["industry_id"],
                spaceExplain=update_space_explain,
                spaceId=space_manage_id,
            )
            self._assert_common_response(response, "编辑空间")

        with allure.step("步骤5：空间结项"):
            response = self.api_space_manage.finish(spaceManageId=space_manage_id)
            self._assert_common_response(response, "空间结项")

        with allure.step("步骤6：查询空间列表并校验编辑/结项结果"):
            response = self.api_space_manage.page(
                spaceName=fixed_space_name,
                spaceStatus="",
                spaceType="",
                pageIndex=1,
                pageSize=20,
            )
            response_body = self._assert_common_response(response, "查询空间列表(结项后)")

            data = response_body.get("data") or {}
            space_list = data.get("list") or []
            target_space = self._find_target_space(space_list, fixed_space_name)
            assert target_space is not None, f"结项后未查询到目标空间: {fixed_space_name}"

            assert str(target_space.get("industryName")) == str(updated_industry["industry_name"]), (
                f"结项后industryName不一致: api={target_space.get('industryName')}, "
                f"expect={updated_industry['industry_name']}"
            )
            assert int(target_space.get("spaceStatus")) == 2, f"结项后spaceStatus不是已结项: {target_space.get('spaceStatus')}"
            assert str(target_space.get("spaceExplain")) == update_space_explain, (
                f"结项后spaceExplain不一致: api={target_space.get('spaceExplain')}, expect={update_space_explain}"
            )
            self._attach_json({"target": target_space}, "结项后查询命中数据")

        with allure.step("步骤7：空间重启"):
            response = self.api_space_manage.restart(spaceManageId=space_manage_id)
            self._assert_common_response(response, "空间重启")

        with allure.step("步骤8：查询空间列表并校验空间为进行中状态"):
            response = self.api_space_manage.page(
                spaceName=fixed_space_name,
                spaceStatus="",
                spaceType="",
                pageIndex=1,
                pageSize=20,
            )
            response_body = self._assert_common_response(response, "查询空间列表(重启后)")

            data = response_body.get("data") or {}
            space_list = data.get("list") or []
            target_space = self._find_target_space(space_list, fixed_space_name)
            assert target_space is not None, f"重启后未查询到目标空间: {fixed_space_name}"
            assert int(target_space.get("spaceStatus")) == 1, f"重启后spaceStatus不是进行中: {target_space.get('spaceStatus')}"
            self._attach_json({"target": target_space}, "重启后查询命中数据")

        ctx.update(
            {
                "space_manage_id": space_manage_id,
                "space_cn_name": space_cn_name,
                "space_type_value": space_type_value,
                "current_industry_id": current_industry_id,
                "updated_industry": updated_industry,
            }
        )

    def _run_member_machine_flow(self):
        ctx = self.__class__.flow_context
        space_manage_id = ctx.get("space_manage_id")
        timestamp = ctx["timestamp"]
        assertions.assert_is_not_none(space_manage_id, "前置步骤未准备好space_manage_id，请先执行test_space_lifecycle")

        with allure.step("步骤9：查询成员管理，校验登录用户名存在"):
            response = self.api_user_info.query_space_users(spaceManageId=space_manage_id, pageIndex=1, pageSize=200)
            response_body = self._assert_common_response(response, "查询成员管理")

            data = response_body.get("data") or {}
            user_list = data.get("list") or []
            user_names = [str(item.get("userName", "")) for item in user_list]
            assert api_login.username in user_names, (
                f"成员管理中未找到登录用户: {api_login.username}, 当前成员userName={user_names}"
            )
            self._attach_json({"userNames": user_names}, "成员管理用户名列表")

        with allure.step("步骤10：下拉成员，提取任意用户的userId和realName"):
            response = self.api_user_info.query_by_phone_or_username(phone="", spaceManageId=space_manage_id, pageIndex=1, pageSize=50)
            response_body = self._assert_common_response(response, "下拉成员")

            data = response_body.get("data") or {}
            user_list = data.get("list") or []
            assert user_list, "下拉成员返回为空，无法继续添加成员步骤"

            target_user = user_list[0]
            selected_user_id = target_user.get("userId")
            selected_real_name = target_user.get("realName")
            assertions.assert_is_not_none(selected_user_id, "下拉成员缺少userId")
            assertions.assert_is_not_none(selected_real_name, "下拉成员缺少realName")

            self._attach_json(
                {"selectedUser": {"userId": selected_user_id, "realName": selected_real_name}},
                "下拉成员提取数据"
            )

        with allure.step("步骤11：添加成员"):
            add_payload = {
                "spaceManageId": space_manage_id,
                "userIds": [str(selected_user_id)],
            }
            self._attach_json(add_payload, "添加成员请求参数")

            response = self.api_space_manage.add_space_user(
                spaceManageId=space_manage_id,
                userIds=[str(selected_user_id)],
            )
            self._assert_common_response(response, "添加成员")

        with allure.step("步骤12：查询成员管理并校验新增成员"):
            response = self.api_user_info.query_space_users(spaceManageId=space_manage_id, pageIndex=1, pageSize=200)
            response_body = self._assert_common_response(response, "查询成员管理(添加后)")

            data = response_body.get("data") or {}
            user_list = data.get("list") or []
            user_ids = [str(item.get("userId")) for item in user_list if item.get("userId") is not None]
            real_names = [str(item.get("realName")) for item in user_list if item.get("realName") is not None]

            assert str(selected_user_id) in user_ids, (
                f"成员管理中未找到新增userId: {selected_user_id}, 当前userIds={user_ids}"
            )
            assert str(selected_real_name) in real_names, (
                f"成员管理中未找到新增realName: {selected_real_name}, 当前realNames={real_names}"
            )
            self._attach_json({"userIds": user_ids, "realNames": real_names}, "添加后成员管理列表")

        with allure.step("步骤13：移除成员"):
            remove_payload = {
                "spaceManageId": space_manage_id,
                "userId": str(selected_user_id),
            }
            self._attach_json(remove_payload, "移除成员请求参数")

            response = self.api_space_manage.del_space_user(
                spaceManageId=space_manage_id,
                userId=str(selected_user_id),
                userIds=None,
            )
            self._assert_common_response(response, "移除成员")

        with allure.step("步骤14：查询成员管理并校验成员已移除"):
            response = self.api_user_info.query_space_users(spaceManageId=space_manage_id, pageIndex=1, pageSize=200)
            response_body = self._assert_common_response(response, "查询成员管理(移除后)")

            data = response_body.get("data") or {}
            user_list = data.get("list") or []
            user_ids = [str(item.get("userId")) for item in user_list if item.get("userId") is not None]

            assert str(selected_user_id) not in user_ids, (
                f"移除后成员仍存在: userId={selected_user_id}, 当前userIds={user_ids}"
            )
            self._attach_json({"userIds": user_ids, "removedUserId": str(selected_user_id)}, "移除后成员管理列表")

        with allure.step("步骤15：下拉机器列表，提取machineId和sn"):
            machine_page_resp = self.api_machine.page(spaceId=None, deviceNo="", status=0, pageIndex=1, pageSize=9999)
            machine_page_body = self._assert_common_response(machine_page_resp, "下拉机器列表")

            machine_data = machine_page_body.get("data") or {}
            machine_list = machine_data.get("list") or []

            assert machine_list, "下拉机器列表为空，无法继续添加机器步骤"
            target_machine = machine_list[0]
            selected_machine_id = target_machine.get("machineId")
            selected_machine_sn = target_machine.get("sn")
            assertions.assert_is_not_none(selected_machine_id, "下拉机器列表缺少machineId")

            self._attach_json(
                {"selectedMachine": {"machineId": selected_machine_id, "sn": selected_machine_sn}},
                "下拉机器提取数据"
            )

        with allure.step("步骤16：添加机器"):
            add_machine_payload = {
                "machineId": str(selected_machine_id),
                "spaceId": str(space_manage_id),
            }
            self._attach_json(add_machine_payload, "添加机器请求参数")

            add_machine_resp = self.api_machine.add(machineId=str(selected_machine_id), spaceId=str(space_manage_id))
            self._assert_common_response(add_machine_resp, "添加机器")

        with allure.step("步骤17：查询机器管理列表并校验新增机器存在"):
            machine_page_resp = self.api_machine.page(spaceId=str(space_manage_id), deviceNo="", status=None, pageIndex=1, pageSize=9999)
            machine_page_body = self._assert_common_response(machine_page_resp, "查询机器管理列表")

            machine_data = machine_page_body.get("data") or {}
            machine_list = machine_data.get("list") or []
            machine_ids = [str(item.get("machineId")) for item in machine_list if item.get("machineId") is not None]
            assert str(selected_machine_id) in machine_ids, (
                f"机器管理列表未找到新增machineId: {selected_machine_id}, 当前machineIds={machine_ids}"
            )
            self._attach_json({"machineIds": machine_ids, "spaceId": str(space_manage_id)}, "机器管理列表数据")

        with allure.step("步骤18：下载token并覆盖testdata/Token.txt"):
            token_path = self.api_machine.machine_token_download(machine_id=str(selected_machine_id))
            assert token_path, "token下载返回路径为空"
            self._attach_json(
                {
                    "machineId": str(selected_machine_id),
                    "tokenPath": token_path,
                },
                "token下载覆盖结果"
            )

        with allure.step("步骤19：移除机器"):
            remove_machine_payload = {
                "machineId": str(selected_machine_id),
                "spaceId": str(space_manage_id),
            }
            self._attach_json(remove_machine_payload, "移除机器请求参数")
            remove_machine_resp = self.api_machine.remove(machineId=str(selected_machine_id), spaceId=str(space_manage_id))
            self._assert_common_response(remove_machine_resp, "移除机器")

        with allure.step("步骤20：查询机器管理列表确认机器已移除"):
            machine_page_resp = self.api_machine.page(spaceId=str(space_manage_id), deviceNo="", status=None, pageIndex=1, pageSize=9999)
            machine_page_body = self._assert_common_response(machine_page_resp, "查询机器管理列表(移除后)")

            machine_data = machine_page_body.get("data") or {}
            machine_list = machine_data.get("list") or []
            machine_ids = [str(item.get("machineId")) for item in machine_list if item.get("machineId") is not None]
            assert str(selected_machine_id) not in machine_ids, (
                f"机器移除后仍存在: machineId={selected_machine_id}, 当前machineIds={machine_ids}"
            )
            self._attach_json({"machineIds": machine_ids}, "移除后机器管理列表")

        ctx.update(
            {
                "selected_user_id": selected_user_id,
                "selected_real_name": selected_real_name,
                "selected_machine_id": selected_machine_id,
                "selected_machine_sn": selected_machine_sn,
            }
        )

    def _run_product_flow(self):
        ctx = self.__class__.flow_context
        space_manage_id = ctx.get("space_manage_id")
        fixed_space_manage_id_for_select = "2057273944318660610"
        product_code = ctx["product_code"]
        product_show_name = ctx["product_show_name"]
        updated_product_show_name = ctx["updated_product_show_name"]
        assertions.assert_is_not_none(space_manage_id, "前置步骤未准备好space_manage_id，请先执行test_space_lifecycle")

        with allure.step("步骤21：添加产品"):
            add_product_payload = {
                "spaceManageId": str(space_manage_id),
                "productCode": product_code,
                "showName": product_show_name,
                "referenceProductCode": "",
                "copyLabel": True,
            }
            self._attach_json(add_product_payload, "添加产品请求参数")

            add_product_resp = self.api_product.add(
                spaceManageId=str(space_manage_id),
                productCode=product_code,
                showName=product_show_name,
                referenceProductCode="",
                copyLabel=True,
            )
            self._assert_common_response(add_product_resp, "添加产品")

        with allure.step("步骤22：查询产品列表并提取productInfoId"):
            product_page_resp = self.api_product.page(
                spaceManageId=str(space_manage_id),
                source="",
                queryText="",
                pageIndex=1,
                pageSize=10,
            )
            product_page_body = self._assert_common_response(product_page_resp, "查询产品列表")

            product_data = product_page_body.get("data") or {}
            product_list = product_data.get("list") or []
            target_product = None
            for item in product_list:
                if str(item.get("productCode")) == product_code:
                    target_product = item
                    break

            assert target_product is not None, f"未找到刚添加产品: productCode={product_code}"
            product_info_id = target_product.get("productInfoId")
            assertions.assert_is_not_none(product_info_id, "查询产品列表未返回productInfoId")
            self._attach_json({"targetProduct": target_product}, "产品列表命中数据")

        with allure.step("步骤23：编辑产品"):
            update_product_payload = {
                "spaceManageId": str(space_manage_id),
                "spaceId": str(space_manage_id),
                "productInfoId": str(product_info_id),
                "showName": updated_product_show_name,
            }
            self._attach_json(update_product_payload, "编辑产品请求参数")

            update_product_resp = self.api_product.update(
                spaceManageId=str(space_manage_id),
                spaceId=str(space_manage_id),
                productInfoId=str(product_info_id),
                showName=updated_product_show_name,
            )
            self._assert_common_response(update_product_resp, "编辑产品")

        with allure.step("步骤24：产品下拉并校验productInfoId存在"):
            select_payload = {
                "data": {"spaceManageId": fixed_space_manage_id_for_select},
                "page": {"pageIndex": 1, "pageSize": 9999},
            }
            self._attach_json(select_payload, "产品下拉请求参数")

            product_select_resp = self.api_product.select(
                spaceManageId=fixed_space_manage_id_for_select,
                ignoreProductInfoIdList=[],
                pageIndex=1,
                pageSize=9999,
            )
            product_select_body = self._assert_common_response(product_select_resp, "产品下拉")

            select_data = product_select_body.get("data") or {}
            select_list = select_data.get("list") or []
            select_product_info_ids = [
                str(item.get("productInfoId"))
                for item in select_list
                if item.get("productInfoId") is not None
            ]
            assert str(product_info_id) in select_product_info_ids, (
                f"产品下拉未找到步骤22提取的productInfoId: {product_info_id}, "
                f"当前productInfoIds={select_product_info_ids}"
            )
            self._attach_json({"productInfoIds": select_product_info_ids}, "产品下拉列表")

        with allure.step("步骤25：删除产品"):
            delete_product_resp = self.api_product.delete(productInfoId=str(product_info_id))
            self._assert_common_response(delete_product_resp, "删除产品")

        with allure.step("步骤26：查询产品管理列表并校验产品已删除"):
            product_page_resp = self.api_product.page(
                spaceManageId=str(space_manage_id),
                source="",
                queryText="",
                pageIndex=1,
                pageSize=10,
            )
            product_page_body = self._assert_common_response(product_page_resp, "查询产品管理列表(删除后)")

            product_data = product_page_body.get("data") or {}
            product_list = product_data.get("list") or []
            product_codes = [str(item.get("productCode")) for item in product_list if item.get("productCode") is not None]
            assert product_code not in product_codes, (
                f"产品删除后仍存在: productCode={product_code}, 当前productCodes={product_codes}"
            )
            self._attach_json({"productCodes": product_codes, "deletedProductCode": product_code}, "删除后产品列表")

    def _run_project_overview_flow(self):
        space_manage_id = self._resolve_fixed_space_manage_id()

        with allure.step("步骤27：调用项目详情接口，校验状态码与msg"):
            response = self.api_space_manage.detail(spaceManageId=str(space_manage_id))
            self._assert_status_and_msg_success(response, "项目详情")

        with allure.step("步骤28：调用产品统计数量查询接口，校验状态码与msg"):
            response = self.api_product.statistics_num(
                spaceManageId=str(space_manage_id),
                productInfoId="",
            )
            self._assert_status_and_msg_success(response, "产品统计数量查询")

        with allure.step("步骤29：调用获取设备方案数接口，校验状态码与msg"):
            response = self.api_machine.scheme_count(
                spaceId=str(space_manage_id),
                machineId="",
            )
            self._assert_status_and_msg_success(response, "获取设备方案数")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--alluredir=./allure-results"])
