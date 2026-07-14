"""Classify runtime failures for later analysis and regression testing."""

from __future__ import annotations


def classify_failure(message: str, *, preflight_failed: bool = False) -> dict[str, object]:
    text = (message or "").lower()
    if preflight_failed and any(
        marker in message
        for marker in (
            "QgsProcessingFeedback",
            "featureCount",
            "QgsVectorFileWriter",
            "mapLayersByName",
            "addVectorLayer",
            "qgis.PyQt",
            "PREDICATE",
            "JOIN_FIELDS",
            "AGGREGATES",
            "参数名是 JOIN",
            "isGeosEmpty",
        )
    ):
        return _result("generated_code_api", "生成代码命中了已知的 PyQGIS API 误用模式。", False)
    if preflight_failed or "代码与预期输出不一致" in message:
        return _result("output_contract", "生成代码声明了未实际写入的输出文件。", False)
    if "fts5:" in text:
        return _result("search_query", "消息搜索字符串包含未转义的 FTS5 语法字符。", False)
    if (
        "artifact 必须" in message
        or "artifact 不是" in message
        or "artifact 不是可恢复" in message
        or "stage_name 必须" in message
    ):
        return _result("tool_arguments", "模型生成的工具参数格式不符合工具 schema。", False)
    if "pipeline 阶段顺序" in text or "pipeline stage" in text:
        return _result("pipeline_stage", "Pipeline 阶段顺序或阶段产物不符合约束。", False)
    if "无效几何" in message or "invalid geometr" in text or "null geometry" in text:
        return _result("invalid_geometry", "输入包含空几何或无效几何，应在分析中排除。", False)
    if "缺少预期输出文件" in message or "missing expected output" in text:
        return _result("missing_output", "代码执行结束但没有生成声明的输出文件。", False)
    if "timeout" in text or "timed out" in text or "超时" in message:
        return _result("timeout", "模型、工具或 GIS 运算超过等待时限。", True)
    if "no user query found" in text:
        return _result("llm_context_missing", "模型请求历史中缺少 user 角色消息。", False)
    if "rate limit" in text or "429" in text:
        return _result("rate_limit", "模型服务触发速率限制。", True)
    if "找不到图层" in message or "layer not found" in text:
        return _result("layer_not_found", "生成代码引用了不存在或名称不匹配的图层。", False)
    if "字段不存在" in message or "缺少" in message and "字段" in message:
        return _result("field_not_found", "生成代码引用了不存在或不匹配的字段。", False)
    if any(marker in message for marker in ("PREDICATE", "JOIN_FIELDS", "AGGREGATES")):
        return _result(
            "processing_parameters",
            "生成代码中的 Processing 参数名、类型或枚举值与算法定义不匹配。",
            False,
        )
    if "source layer for join" in text or "参数 join" in text:
        return _result(
            "processing_parameters",
            "空间连接缺少 JOIN 输入，或错用了其他算法的参数名。",
            False,
        )
    if "isgeosempty" in text:
        return _result("generated_code_api", "生成代码调用了不存在的 PyQGIS API。", False)
    if "syntaxerror" in text:
        return _result("syntax_error", "生成的 Python 代码存在语法错误。", False)
    if "nameerror" in text or "is not defined" in text:
        return _result("name_error", "生成代码使用了未定义或未导入的名称。", False)
    if "qgsprocessingexception" in text or "processingexception" in text:
        return _result("processing_error", "QGIS Processing 算法执行失败。", False)
    return _result("unknown", "尚未匹配到已知错误类别，需要人工归因。", False)


def _result(error_code: str, cause: str, retryable: bool) -> dict[str, object]:
    return {"error_code": error_code, "cause": cause, "retryable": retryable}
