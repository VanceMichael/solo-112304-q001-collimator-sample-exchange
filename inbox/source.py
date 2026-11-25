"""演练来源与合作端的 HTTP 访问。"""
from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class SourceError(Exception):
    """来源访问异常。"""


class Gone(SourceError):
    """上游已覆盖该测量块（410）。"""


class Source:
    def __init__(self, origin: str, timeout: float = 5.0):
        self.origin = origin.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str):
        request = Request(self.origin + path)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read().decode("utf-8")
        except HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def get_json(self, path: str):
        status, body = self._get(path)
        if status == 404:
            return None
        if status != 200:
            raise SourceError(f"GET {path} 返回 {status}: {body}")
        return json.loads(body)

    def get_raw(self, href: str) -> str:
        """取设备原件。每次调用都是一次导出，计入工厂导出配额。"""
        status, body = self._get(href)
        if status == 410:
            raise Gone(href)
        if status != 200:
            raise SourceError(f"GET {href} 返回 {status}: {body}")
        return body

    def post_json(self, path: str, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(self.origin + path, data=data,
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"message": raw}

    # 业务端点
    def config(self):
        return self.get_json("/gauges/config")

    def index(self):
        return self.get_json("/gauges/index")

    def bases(self):
        return self.get_json("/gauges/bases")

    def orders(self):
        return self.get_json("/quality/orders")

    def validity(self):
        """检具对来源键有效性的声明；未提供时为 None。"""
        return self.get_json("/gauges/validity")

    def buyer_receipt(self, request_id: str):
        """按请求号查合作端回执；404 表示该时刻查不到。"""
        return self.get_json("/buyer/intake?" + urlencode({"request": request_id}))

    def buyer_submit(self, payload: dict):
        return self.post_json("/buyer/intake", payload)
