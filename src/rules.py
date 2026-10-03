from .domain import DomainError

ENTITY_TYPE = "space_conjunction"
INITIAL_STATUS = "pending"
CREATE_ROLES = {"analyst"}
SOURCE_ROLES = {"analyst", "operator"}
ACTION_ROLES = {
    "assess": {"analyst"},
    "record_opinion": {"operator"},
    "approve": {"coordinator"},
    "execute": {"operator"},
    "report_execution_failure": {"operator"},
    "resolve": {"coordinator"},
    "cancel": {"coordinator"},
    "report_revision": {"analyst"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"approve", "execute", "report_execution_failure", "resolve", "cancel", "report_revision"}


def observation_version(current):
    return int(current.get("observation_version", 1))


def invalidate_for_observation(current):
    """轨道观测一更新，旧评估、规避方案、运营方签字和执行记录全部失效，需要重走审批。

    观测版本号递增，清空评估、已批准规避方案、运营方意见和执行记录，
    状态回到 pending，由分析员重新评估、协调员重新批准。
    """
    current["observation_version"] = observation_version(current) + 1
    current.pop("assessment", None)
    current.pop("approved_maneuver", None)
    current["opinions"] = []
    current.pop("execution", None)
    current.pop("command_ref", None)
    current.pop("resolution", None)
    current["conflict"] = False
    return "pending", {
        "observation_version": current["observation_version"],
        "invalidated": ["assessment", "approved_maneuver", "opinions", "execution"],
    }


def assess(payload):
    ratio = float(payload.get("miss_distance_m", 0)) / max(float(payload.get("covariance_m", 1)), 1.0)
    tca_hours = float(payload.get("hours_to_tca", 24))
    severity = max(0.0, 100.0 - min(95.0, ratio * 20.0))
    urgency = max(0.0, min(20.0, (24.0 - tca_hours) * 0.8))
    score = round(min(100.0, severity + urgency), 2)
    if score >= 80:
        level = "high"
    elif score >= 50:
        level = "medium"
    else:
        level = "low"
    return {"score": score, "level": level, "distance_to_covariance_ratio": round(ratio, 3)}


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _require_number(payload, name, minimum=None):
    try:
        value = float(payload[name])
    except (KeyError, TypeError, ValueError):
        raise DomainError("field_required", "%s 不能为空" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    return value


def _require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "assess":
        _need_status(item, {"pending", "assessed"})
        age = float(current.get("track_age_hours", 0))
        if age > 6:
            raise DomainError("stale_track", "轨道数据已过期，不能用于风险评估")
        current["hours_to_tca"] = float(payload.get("hours_to_tca", current.get("hours_to_tca", 24)))
        result = assess(current)
        result["observation_version"] = observation_version(current)
        current["assessment"] = result
        return "assessed", current, {"assessment": result, "actor": actor}

    if action == "report_revision":
        _need_status(item, {"pending", "assessed", "coordinating", "executing"})
        revision = {
            "observed_at": _require_text(payload, "observed_at"),
            "miss_distance_m": _require_number(payload, "miss_distance_m", 0),
            "covariance_m": _require_number(payload, "covariance_m", 0.001),
            "source": _require_text(payload, "source"),
        }
        if revision["covariance_m"] <= 0:
            raise DomainError("invalid_covariance", "协方差必须大于零")
        # 轨道观测一更新，旧评估和规避方案即失效，需要重走审批。
        new_status, invalidated = invalidate_for_observation(current)
        revision["observation_version"] = invalidated["observation_version"]
        current.setdefault("revisions", []).append(revision)
        current["miss_distance_m"] = revision["miss_distance_m"]
        current["covariance_m"] = revision["covariance_m"]
        return new_status, current, {"revision": revision, "invalidated": invalidated}

    if action == "record_opinion":
        _need_status(item, {"assessed", "coordinating"})
        opinion = _require_text(payload, "opinion").lower()
        if opinion not in {"approve", "reject", "request_review"}:
            raise DomainError("invalid_opinion", "意见必须是 approve、reject 或 request_review")
        operator = _require_text(payload, "operator")
        entry = {
            "operator": operator,
            "opinion": opinion,
            "reason": payload.get("reason", ""),
            "observation_version": observation_version(current),
        }
        current.setdefault("opinions", []).append(entry)
        if opinion in {"reject", "request_review"}:
            current["conflict"] = True
        return status, current, {"opinion": entry}

    if action == "approve":
        _need_status(item, {"assessed"})
        assessment = current.get("assessment")
        if not assessment or int(assessment.get("observation_version", 0)) != observation_version(current):
            raise DomainError("stale_assessment", "评估已过期，需要重新评估", 409)
        if current.get("conflict"):
            raise DomainError("unresolved_conflict", "存在未解决的运营方冲突意见", 409)
        fuel = _require_number(payload, "fuel_cost_m_s", 0)
        budget = float(current.get("fuel_budget_m_s", 0))
        if fuel > budget:
            raise DomainError("fuel_budget_exceeded", "规避燃料超过预算", 409)
        window = _require_text(payload, "maneuver_window")
        current["approved_maneuver"] = {
            "fuel_cost_m_s": fuel,
            "maneuver_window": window,
            "observation_version": observation_version(current),
        }
        return "coordinating", current, {"approved_maneuver": current["approved_maneuver"]}

    if action == "execute":
        _need_status(item, {"coordinating"})
        command_ref = _require_text(payload, "command_ref")
        execution = current.setdefault("execution", {"status": "not_started", "attempts": []})
        attempt = len(execution.get("attempts", [])) + 1
        record = {"attempt": attempt, "command_ref": command_ref, "status": "succeeded"}
        execution.setdefault("attempts", []).append(record)
        execution["status"] = "succeeded"
        current["command_ref"] = command_ref
        return "executing", current, {"command_ref": command_ref, "attempt": attempt}

    if action == "report_execution_failure":
        _need_status(item, {"coordinating", "executing"})
        reason = _require_text(payload, "reason")
        execution = current.setdefault("execution", {"status": "not_started", "attempts": []})
        attempt = len(execution.get("attempts", [])) + 1
        record = {"attempt": attempt, "status": "failed", "reason": reason}
        execution.setdefault("attempts", []).append(record)
        execution["status"] = "failed"
        # 执行失败后保留已批准的机动窗口，只重试未完成的动作（execute），不重走审批。
        current.pop("command_ref", None)
        window = current.get("approved_maneuver", {}).get("maneuver_window")
        return "coordinating", current, {"failure": record, "maneuver_window": window, "retry": True}

    if action == "resolve":
        _need_status(item, {"executing"})
        execution = current.get("execution") or {}
        if execution.get("status") != "succeeded":
            raise DomainError("execution_incomplete", "规避动作尚未成功完成，不能结束", 409)
        report_ref = _require_text(payload, "report_ref")
        current["resolution"] = {"report_ref": report_ref, "resolved_by": actor}
        return "resolved", current, {"report_ref": report_ref}

    if action == "cancel":
        _need_status(item, {"pending", "assessed"})
        reason = _require_text(payload, "reason")
        current["cancellation"] = {"reason": reason, "cancelled_by": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
