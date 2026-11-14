#!/usr/bin/env python3
"""维度属性准入链路验收脚本。

覆盖：启动巡检、下架准入、枚举改名换版、空枚举、回滚、重复启用、并发换版、
写入失败回滚，以及模板详情 / 聚合结果 / 发布记录三方互查。
仅依赖标准库；对 LOCAL_TEST=1 启动的后端发真实 HTTP 请求。
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime

BASE = "http://127.0.0.1:8000"

PASS = 0
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    global PASS
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAILURES.append(f"{name} :: {detail}")
        print(f"  FAIL  {name} :: {detail}")


def request(method: str, path: str, body: dict | None = None, expect_status: int = 200):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode())
        if expect_status is not None and exc.code != expect_status:
            raise AssertionError(f"{method} {path} 期望 {expect_status}，实际 {exc.code}: {payload}")
        return exc.code, payload


def reset() -> None:
    request("POST", "/api/_test/reset")


def faults(points: list[str]) -> None:
    request("POST", "/api/_test/faults", {"points": points})


def find_id(module: str, code_field: str, code: str) -> int:
    _, page = request("GET", f"/api/{module}?size=200")
    for row in page["items"]:
        if row.get(code_field) == code:
            return int(row["id"])
    raise AssertionError(f"{module} 中找不到 {code}={code}")


def action(module: str, entry_id: int, values: dict):
    return request("POST", f"/api/{module}/{entry_id}/actions", {"values": values}, expect_status=None)


def create(module: str, values: dict):
    return request("POST", f"/api/{module}", {"values": values})


def create_property(pid: str, enum: str, data_type: str = "varchar") -> int:
    _, result = create("dimprop", {
        "属性编号": pid,
        "维度名称": f"维度-{pid}",
        "属性名称": f"属性-{pid}",
        "数据类型": data_type,
        "枚举值列表": enum,
        "关联数据源": f"dw.{pid.lower()}",
    })
    assert result["ok"], result
    entry_id = result["entry"]["id"]
    _, result = action("dimprop", entry_id, {"action": "发布属性"})
    assert result["ok"], result
    return entry_id


def create_template(tid: str, pid: str) -> int:
    _, result = create("queryboard", {
        "模板编号": tid,
        "指标名称": f"metric_{tid.lower()}",
        "维度字段": pid,
        "bound_property_id": pid,
    })
    assert result["ok"], result
    return int(result["entry"]["id"])


def enable_template(tpl_id: int):
    return action("queryboard", tpl_id, {"action": "启用模板"})


def aggregate_rows(tpl_id: int):
    status, payload = request("GET", f"/api/queryboard/{tpl_id}/aggregate", expect_status=200)
    return payload["rows"]


def unique(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%H%M%S%f')[:9]}"


# --------------------------------------------------------------- 场景 0：启动巡检
def scenario_reconcile() -> None:
    print("\n[场景0] 启动巡检：复刻异常样例 DIMP-CHANNEL@v3 已下线")
    reset()
    tpl_id = find_id("queryboard", "模板编号", "QUER-SALES_DIM")
    _, detail = request("GET", f"/api/queryboard/{tpl_id}")
    check("模板停在模板验证中", detail["status"] == "模板验证中", detail["status"])
    check("草稿 data_type=varchar 保留", detail["snapshot_data_type"] == "varchar", detail.get("snapshot_data_type"))
    check("草稿 source_connection 保留", detail["snapshot_source_connection"] == "sales_dw.channel_dim", detail.get("snapshot_source_connection"))
    check("草稿枚举快照保留", detail["snapshot_enum_values"] == ["CARD", "CASH"], detail.get("snapshot_enum_values"))
    err = detail["validation_error"]
    check("错误码 DIMENSION_PROPERTY_RETIRED", err and err["code"] == "DIMENSION_PROPERTY_RETIRED", err)
    check("上下文 property_id", err and err["context"].get("property_id") == "DIMP-CHANNEL", err)
    check("上下文 retired_version=v3", err and err["context"].get("retired_version") == "v3", err)
    status, payload = request("GET", f"/api/queryboard/{tpl_id}/aggregate", expect_status=409)
    check("revenue_by_channel 不再产生聚合", status == 409 and payload["detail"]["code"] == "DIMENSION_PROPERTY_RETIRED", payload)
    # 重复启用仍然被拦，结论稳定。
    _, result = enable_template(tpl_id)
    check("重复启用被准入拦截", not result["ok"] and result["error"]["code"] == "DIMENSION_PROPERTY_RETIRED", result)
    check("拦截后仍在模板验证中", result["entry"]["status"] == "模板验证中", result["entry"]["status"])


# --------------------------------------------------------------- 场景 1：下架准入
def scenario_retire() -> None:
    print("\n[场景1] 属性下架后阻止模板新启用，草稿保留并标记失效，缓存清理")
    reset()
    pid = unique("DIMP-RET")
    prop_id = create_property(pid, "A,B")
    tpl_pk = create_template(unique("QUER-RET"), pid)
    _, result = enable_template(tpl_pk)
    check("属性发布时模板可启用", result["ok"] and result["entry"]["status"] == "已启用", result)
    before = aggregate_rows(tpl_pk)
    check("启用后产生两条枚举聚合", len(before) == 2, before)

    _, result = action("dimprop", prop_id, {"action": "下线属性"})
    check("属性下线成功", result["ok"] and result["entry"]["status"] == "已下线", result)

    _, detail = request("GET", f"/api/queryboard/{tpl_pk}")
    check("已启用模板被打回模板验证中", detail["status"] == "模板验证中", detail["status"])
    check("模板标记 abnormal", detail["abnormal"] is True, detail.get("abnormal"))
    check("草稿字段全部保留", detail["snapshot_data_type"] == "varchar" and detail["snapshot_enum_values"] == ["A", "B"], detail)
    check("错误码与上下文正确", detail["validation_error"]["code"] == "DIMENSION_PROPERTY_RETIRED"
          and detail["validation_error"]["context"]["property_id"] == pid
          and detail["validation_error"]["context"]["retired_version"] == "v1", detail["validation_error"])
    status, payload = request("GET", f"/api/queryboard/{tpl_pk}/aggregate", expect_status=409)
    check("旧聚合缓存已清理、不再返回旧枚举结果", status == 409, payload)

    _, result = enable_template(tpl_pk)
    check("下架后新启用被阻止", not result["ok"], result)
    check("失败时模板保持不可用", result["entry"]["status"] == "模板验证中", result["entry"]["status"])


# --------------------------------------------------------------- 场景 2：枚举改名 + 换版
def scenario_rename() -> None:
    print("\n[场景2] 枚举改名：重新发布生成新版本，旧缓存不能命中，模板读同一发布版本")
    reset()
    pid = unique("DIMP-REN")
    create_property(pid, "CARD,CASH")
    tpl_pk = create_template(unique("QUER-REN"), pid)
    _, result = enable_template(tpl_pk)
    check("v1 启用成功", result["ok"], result)
    old_rows = aggregate_rows(tpl_pk)
    check("v1 聚合含 CARD/CASH", {r[pid] for r in old_rows} == {"CARD", "CASH"}, old_rows)

    prop_pk = find_id("dimprop", "属性编号", pid)
    _, result = action("dimprop", prop_pk, {"action": "发布属性", "枚举值列表": "CARD_V2,CASH_V2"})
    check("重新发布成功", result["ok"], result)
    _, versions = request("GET", f"/api/dimprop/property/{pid}/versions")
    labels = [(v["version"], v["status"]) for v in versions["items"]]
    check("生成 v2 且 v1 退役", ("v1", "retired") in labels and ("v2", "published") in labels, labels)

    _, detail = request("GET", f"/api/queryboard/{tpl_pk}")
    check("引用 v1 的模板回到模板验证中", detail["status"] == "模板验证中", detail["status"])
    check("模板仍绑定 v1（只读取同一发布版本，不被静默漂移）", detail["bound_version"] == "v1", detail["bound_version"])
    check("快照仍是旧枚举，草稿不被覆盖", detail["snapshot_enum_values"] == ["CARD", "CASH"], detail["snapshot_enum_values"])
    check("错误标记为版本退役", detail["validation_error"]["code"] == "DIMENSION_PROPERTY_RETIRED", detail["validation_error"])

    # 把模板改绑新版本后启用（模拟重新走验证并接受新版本）。
    _, rebind = create("queryboard", {
        "模板编号": unique("QUER-REN2"),
        "指标名称": "metric_ren2",
        "维度字段": pid,
        "bound_property_id": pid,
        "bound_version": "v2",
    })
    tpl2 = int(rebind["entry"]["id"])
    check("新模板快照来自 v2", rebind["entry"]["snapshot_enum_values"] == ["CARD_V2", "CASH_V2"], rebind["entry"])
    _, result = enable_template(tpl2)
    check("v2 模板可启用", result["ok"], result)
    new_rows = aggregate_rows(tpl2)
    check("v2 聚合只含新枚举", {r[pid] for r in new_rows} == {"CARD_V2", "CASH_V2"}, new_rows)

    # 旧模板的旧版本缓存键物理上不可能命中 v2 的查询。
    _, old_detail = request("GET", f"/api/queryboard/{tpl_pk}")
    status, payload = request("GET", f"/api/queryboard/{tpl_pk}/aggregate", expect_status=409)
    check("旧模板聚合被拒，旧缓存不残留", status == 409 and payload["detail"]["code"] == "DIMENSION_PROPERTY_RETIRED", payload)


# --------------------------------------------------------------- 场景 3：空枚举
def scenario_empty_enum() -> None:
    print("\n[场景3] 空枚举发布版本：模板验证与启用都被拦截")
    reset()
    pid = unique("DIMP-EMP")
    create_property(pid, "X,Y")
    tpl_pk = create_template(unique("QUER-EMP"), pid)
    _, result = enable_template(tpl_pk)
    check("非空枚举时可启用", result["ok"], result)

    prop_pk = find_id("dimprop", "属性编号", pid)
    _, result = action("dimprop", prop_pk, {"action": "发布属性", "枚举值列表": " , "})
    check("空枚举也允许发布（版本事实成立）", result["ok"], result)
    _, versions = request("GET", f"/api/dimprop/property/{pid}/versions")
    check("空版本 v2 枚举为空列表", versions["items"][-1]["enum_values"] == [], versions["items"][-1])

    # 新模板绑定空枚举版本
    _, created = create("queryboard", {
        "模板编号": unique("QUER-EMP2"),
        "指标名称": "metric_emp2",
        "维度字段": pid,
        "bound_property_id": pid,
        "bound_version": "v2",
    })
    tpl2 = int(created["entry"]["id"])
    _, result = action("queryboard", tpl2, {"action": "提交模板验证"})
    check("验证环节报 DIMENSION_PROPERTY_EMPTY_ENUM",
          not result["ok"] and result["error"]["code"] == "DIMENSION_PROPERTY_EMPTY_ENUM", result)
    check("验证失败停在模板验证中", result["entry"]["status"] == "模板验证中", result["entry"]["status"])
    _, result = enable_template(tpl2)
    check("空枚举模板无法启用", not result["ok"] and result["error"]["code"] == "DIMENSION_PROPERTY_EMPTY_ENUM", result)
    check("模板保持不可用", result["entry"]["status"] == "模板验证中", result["entry"]["status"])


# --------------------------------------------------------------- 场景 4：回滚
def scenario_rollback() -> None:
    print("\n[场景4] 回滚：复制历史版本内容发布新版本，旧版本号与旧缓存不复活")
    reset()
    pid = unique("DIMP-RB")
    create_property(pid, "V1_A")
    tpl_pk = create_template(unique("QUER-RB"), pid)
    _, result = enable_template(tpl_pk)
    check("v1 启用", result["ok"], result)
    prop_pk = find_id("dimprop", "属性编号", pid)
    action("dimprop", prop_pk, {"action": "发布属性", "枚举值列表": "V2_A,V2_B"})
    _, versions = request("GET", f"/api/dimprop/property/{pid}/versions")
    check("换版后 v1 retired、v2 published",
          versions["items"][0]["status"] == "retired" and versions["items"][1]["status"] == "published", versions["items"])

    _, result = request("POST", f"/api/dimprop/{prop_pk}/rollback", {"values": {"target_version": "v1"}})
    check("回滚接口成功", result["ok"], result)
    _, versions = request("GET", f"/api/dimprop/property/{pid}/versions")
    labels = [(v["version"], v["status"], v["enum_values"]) for v in versions["items"]]
    check("生成 v3 且内容回到 v1 枚举", labels[-1] == ("v3", "published", ["V1_A"]), labels)
    check("v1 保持 retired 不复活", labels[0][1] == "retired", labels)
    check("v2 也已退役", labels[1][1] == "retired", labels)

    _, pubs = request("GET", f"/api/dimprop/property/{pid}/publications")
    actions = [(p["version"], p["action"], p.get("from_version")) for p in pubs["items"]]
    check("发布记录含 rollback 且指向 v1", ("v3", "rollback", "v1") in actions, actions)

    # 新模板绑定 v3 后读到的是回滚后的枚举。
    _, created = create("queryboard", {
        "模板编号": unique("QUER-RB2"),
        "指标名称": "metric_rb2",
        "维度字段": pid,
        "bound_property_id": pid,
    })
    tpl2 = int(created["entry"]["id"])
    check("新模板自动绑定 v3", created["entry"]["bound_version"] == "v3", created["entry"])
    _, result = enable_template(tpl2)
    check("v3 模板可启用", result["ok"], result)
    rows = aggregate_rows(tpl2)
    check("聚合枚举为回滚后的 V1_A", {r[pid] for r in rows} == {"V1_A"}, rows)


# --------------------------------------------------------------- 场景 5：并发换版 + 重复启用
def scenario_concurrency() -> None:
    print("\n[场景5] 并发：重复启用与换版同时发生，只能得到一个完整结论")
    reset()
    pid = unique("DIMP-CC")
    prop_pk = create_property(pid, "A,B,C")
    tpl_pk = create_template(unique("QUER-CC"), pid)
    _, result = enable_template(tpl_pk)
    check("初始启用成功", result["ok"], result)

    errors: list[object] = []

    def republish() -> None:
        try:
            action("dimprop", prop_pk, {"action": "发布属性", "枚举值列表": "A2,B2,C2"})
        except Exception as exc:  # noqa: BLE001 - 并发测试需要保留所有异常
            errors.append(exc)

    def re_enable() -> None:
        try:
            enable_template(tpl_pk)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=republish), threading.Thread(target=enable_template and re_enable)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check("并发请求无异常", not errors, errors)

    _, detail = request("GET", f"/api/queryboard/{tpl_pk}")
    _, versions = request("GET", f"/api/dimprop/property/{pid}/versions")
    published = [v for v in versions["items"] if v["status"] == "published"]
    check("恰好一个 published 版本", len(published) == 1, versions["items"])
    # 无论谁先完成，模板与缓存只能指向同一个完整结论：
    # - 若换版先提交：模板必然回到验证中，且无缓存；
    # - 若启用先提交：模板启用且缓存版本与绑定版本严格一致。
    if detail["status"] == "已启用":
        check("启用结论下缓存与绑定版本一致", detail["bound_version"] == published[0]["version"], detail)
        status, payload = request("GET", f"/api/queryboard/{tpl_pk}/aggregate", expect_status=200)
        check("聚合缓存版本一致", payload["version"] == detail["bound_version"], payload)
    else:
        check("换版结论下模板在验证中", detail["status"] == "模板验证中", detail["status"])
        status, payload = request("GET", f"/api/queryboard/{tpl_pk}/aggregate", expect_status=409)
        check("换版结论下无旧缓存", status == 409, payload)
        check("错误指向退役版本", payload["detail"]["code"] == "DIMENSION_PROPERTY_RETIRED", payload)


# --------------------------------------------------------------- 场景 6：任一侧写入失败整体回滚
def scenario_rollback_on_failure() -> None:
    print("\n[场景6] 故障注入：启用提交阶段失败时模板保持不可用且无半成品缓存")
    reset()
    pid = unique("DIMP-FLT")
    create_property(pid, "A,B")
    tpl_pk = create_template(unique("QUER-FLT"), pid)

    faults(["enable_before_commit"])
    _, result = enable_template(tpl_pk)
    faults([])
    check("故障时启用返回失败", not result["ok"], result)
    _, detail = request("GET", f"/api/queryboard/{tpl_pk}")
    check("模板仍是草稿/验证中，未变成已启用", detail["status"] != "已启用", detail["status"])
    status, payload = request("GET", f"/api/queryboard/{tpl_pk}/aggregate", expect_status=409)
    check("没有任何缓存残留", status == 409, payload)

    # 故障清除后同一模板可以正常启用，证明状态没有被半成品污染。
    _, result = enable_template(tpl_pk)
    check("故障恢复后可正常启用", result["ok"] and result["entry"]["status"] == "已启用", result)
    check("聚合正常产出", len(aggregate_rows(tpl_pk)) == 2, "缓存行")

    print("\n[场景6b] 属性下线联动写入失败：属性状态与模板状态整体回滚")
    prop_pk = find_id("dimprop", "属性编号", pid)
    faults(["retire_after_version"])
    try:
        _, result = action("dimprop", prop_pk, {"action": "下线属性"})
    finally:
        faults([])
    check("下线动作返回失败", not result["ok"], result)
    _, prop = request("GET", f"/api/dimprop/{prop_pk}")
    check("属性回滚为已发布", prop["status"] == "已发布", prop["status"])
    _, versions = request("GET", f"/api/dimprop/property/{pid}/versions")
    check("版本退役写入也已回滚（v1 仍 published）",
          versions["items"][0]["status"] == "published", versions["items"])
    _, detail = request("GET", f"/api/queryboard/{tpl_pk}")
    check("模板仍为已启用", detail["status"] == "已启用", detail["status"])


# --------------------------------------------------------------- 场景 7：三方互查
def scenario_cross_check() -> None:
    print("\n[场景7] 互查：模板详情、聚合结果、发布记录、版本注册表口径一致")
    reset()
    pid = unique("DIMP-X")
    prop_pk = create_property(pid, "E1,E2")
    tpl_pk = create_template(unique("QUER-X"), pid)
    enable_template(tpl_pk)
    action("dimprop", prop_pk, {"action": "发布属性", "枚举值列表": "E1,E2,E3"})

    _, detail = request("GET", f"/api/queryboard/{tpl_pk}")
    _, versions = request("GET", f"/api/dimprop/property/{pid}/versions")
    _, pubs = request("GET", f"/api/dimprop/property/{pid}/publications")
    status, agg = request("GET", f"/api/queryboard/{tpl_pk}/aggregate", expect_status=409)

    bound = next(v for v in versions["items"] if v["version"] == detail["bound_version"])
    check("模板绑定版本在注册表中存在且为 retired", bound["status"] == "retired", bound)
    check("模板快照与注册表退役版本完全一致",
          detail["snapshot_enum_values"] == bound["enum_values"]
          and detail["snapshot_data_type"] == bound["data_type"]
          and detail["snapshot_source_connection"] == bound["source_connection"],
          (detail, bound))
    check("聚合拒绝原因与模板错误码一致",
          agg["detail"]["code"] == detail["validation_error"]["code"], (agg, detail))
    check("发布记录两次 publish 且第二次 from_version=v1",
          [(p["action"], p.get("from_version")) for p in pubs["items"]] == [("publish", None), ("publish", "v1")],
          pubs["items"])
    check("发布记录版本链与注册表版本链一致",
          [p["version"] for p in pubs["items"]] == [v["version"] for v in versions["items"]],
          (pubs["items"], versions["items"]))


def main() -> int:
    for scenario in (
        scenario_reconcile,
        scenario_retire,
        scenario_rename,
        scenario_empty_enum,
        scenario_rollback,
        scenario_concurrency,
        scenario_rollback_on_failure,
        scenario_cross_check,
    ):
        scenario()
    print(f"\n{'=' * 60}")
    print(f"通过 {PASS} 项，失败 {len(FAILURES)} 项")
    if FAILURES:
        for item in FAILURES:
            print(" -", item)
        return 1
    print("全部验收场景通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
