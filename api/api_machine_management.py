"""
机器管理接口
"""
import time
import shutil
import zipfile
from pathlib import Path

import requests
from api import api_login
from common.Request_Response import ApiClient

env = api_login.url
time_str = time.strftime("%Y%m%d%H%M%S", time.localtime())


class ApiMachine:
    def __init__(self, client: ApiClient):
        self.client = client

    # 获取设备方案数
    def scheme_count(self, spaceId, machineId=None):
        url = f"{env}/machine/schemeCount"
        payload = {"spaceId": spaceId, "machineId": machineId}
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 移除机器
    def remove(self, machineId=None, spaceId=None):
        url = f"{env}/machine/remove"
        payload = {"machineId": machineId, "spaceId": spaceId}
        response = self.client.post_with_retry(url, json=payload)
        return response

    # token文件下载预处理
    def prepare_download(self, cloudDeviceNo):
        url = f"{env}/machine/prepareDownload/{cloudDeviceNo}"
        payload = {}
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 分页查询
    def page(self, pageIndex=1, pageSize=9999, spaceId=None, deviceNo="", status=None):
        url = f"{env}/machine/page"
        data = {"deviceNo": deviceNo, "spaceId": spaceId}
        if status is not None:
            data["status"] = status

        payload = {
            "data": data,
            "page": {"pageIndex": pageIndex, "pageSize": pageSize},
        }

        response = self.client.post_with_retry(url, json=payload)
        return response

    # 添加机器
    def add(self, machineId=None, spaceId=None):
        url = f"{env}/machine/add"
        payload = {"machineId": machineId, "spaceId": spaceId}
        response = self.client.post_with_retry(url, json=payload)
        return response

    # 下载token文件
    def token_download(self, key):
        normalized_key = str(key).strip()
        if not normalized_key:
            raise ValueError("token_download key 不能为空")

        # prepare_download 可能直接返回完整下载地址，优先按完整URL处理。
        if normalized_key.startswith("http://") or normalized_key.startswith("https://") or "://" in normalized_key:
            url = normalized_key
        else:
            url = f"{env}/machine/execute/{normalized_key}"
        response = self.client.get_with_retry(url)
        return response

    def machine_token_download(self, machine_id, save_dir=None):
        """通过 prepareDownload 获取签名下载地址并落地 Token.txt。"""
        if machine_id is None or str(machine_id).strip() == "":
            raise RuntimeError("machine_id 不能为空")
        try:
            prepare_response = self.prepare_download(cloudDeviceNo=machine_id)
            prepare_data = prepare_response.json()
        except Exception as e:
            raise RuntimeError(f"获取下载地址失败，machine_id={machine_id}") from e

        if not prepare_data.get("success", False):
            raise RuntimeError(
                f"获取下载地址失败，machine_id={machine_id}，msg={prepare_data.get('msg', '未知错误')}"
            )

        download_url = str(prepare_data.get("data") or "").strip()
        if not download_url:
            raise RuntimeError(f"下载地址为空，machine_id={machine_id}")

        project_root = Path(__file__).resolve().parents[1]
        target_dir = Path(save_dir) if save_dir else project_root / "testdata"
        target_dir.mkdir(parents=True, exist_ok=True)

        zip_path = target_dir / f"machine_{machine_id}_token.zip"
        extract_dir = target_dir / f"machine_{machine_id}_extracted"
        token_target_path = target_dir / "Token.txt"

        try:
            download_response = requests.get(download_url, stream=True, timeout=30)
            download_response.raise_for_status()
            with zip_path.open("wb") as f:
                for chunk in download_response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            if not zipfile.is_zipfile(zip_path):
                raise RuntimeError(f"下载文件不是有效ZIP，machine_id={machine_id}")

            extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(extract_dir)

            token_file = next((p for p in extract_dir.rglob("accessToken.txt")), None)
            if token_file is None:
                raise RuntimeError(f"ZIP中未找到accessToken.txt，machine_id={machine_id}")

            token_content = token_file.read_text(encoding="utf-8").strip()
            if not token_content:
                raise RuntimeError(f"accessToken.txt内容为空，machine_id={machine_id}")

            token_target_path.write_text(token_content, encoding="utf-8")
            return str(token_target_path)
        finally:
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)
            if zip_path.exists():
                zip_path.unlink()

