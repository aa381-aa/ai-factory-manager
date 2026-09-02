"""
统一响应封装（A2）
==================
文件用途：
    统一所有 API 的成功/失败响应结构，替代各 Blueprint 内散落的手写
    jsonify({...}) 字典，保证对外契约一致：

        成功：{"code": 0, "msg": "ok", "data": {...}}
        失败：{"code": <业务码>, "msg": "<错误说明>"}

    注：错误响应不携带 data 键（与手写错误字典保持一致）；成功响应由
    api_response 统一补全 msg="ok"，便于前端统一处理。

接口：
    api_response(code=0, msg="ok", data=None)  -- 统一成功/带数据响应
    error_response(code, msg)                  -- 统一失败响应（无 data 键）
"""


def api_response(code: int = 0, msg: str = "ok", data=None) -> dict:
    """统一成功/业务响应。返回 {"code", "msg", "data"} 字典。

    参数：
        code: 业务码，0 表示成功（默认）
        msg:  提示信息，默认 "ok"
        data: 业务数据，默认 None

    返回：
        dict：{"code": code, "msg": msg, "data": data}
    """
    return {"code": code, "msg": msg, "data": data}


def error_response(code: int, msg: str) -> dict:
    """统一失败响应。返回 {"code", "msg"} 字典（不含 data 键）。

    参数：
        code: 业务码 / HTTP 状态码（与错误场景一致）
        msg:  错误说明

    返回：
        dict：{"code": code, "msg": msg}
    """
    return {"code": code, "msg": msg}
