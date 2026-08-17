"""Classify runtime failures for later analysis and regression testing."""

from __future__ import annotations


def classify_failure(message: str, *, preflight_failed: bool = False) -> dict[str, object]:
    text = (message or "").lower()
    if "service_unreachable" in text or "无法连接 sam3" in text:
        return _result("sam3_service", "SAM3 服务不可达或连接被重置。", True)
    if "model_not_ready" in text or "模型尚未" in message:
        return _result("sam3_model_not_ready", "SAM3 服务已响应但模型尚未就绪。", True)
    if "no_objects_found" in text or "未找到匹配对象" in message:
        return _result("sam3_no_objects", "SAM3 在当前提示、阈值和范围内没有找到对象。", False)
    if "payload_too_large" in text or "请求快照" in message and "超过" in message:
        return _result("sam3_payload_too_large", "SAM3 请求影像超过上传或处理限制。", False)
    if "invalid_response" in text or "sam3 掩码尺寸" in text:
        return _result("sam3_invalid_response", "SAM3 返回文件为空、损坏或空间尺寸不一致。", False)
    if "参数 json 不完整或被截断" in text or "truncated_tool_arguments" in text:
        return _result(
            "truncated_tool_arguments",
            "模型生成的工具参数超过输出预算或在 JSON 完成前被截断。",
            True,
        )
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
            "InvalidGeometryCheck",
            "QgsColorRampShader",
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
    if (
        "missing_expected_output" in text
        or ("预期输出未通过运行时验证" in message and "不存在" in message)
        or "缺少预期输出文件" in message
        or "missing expected output" in text
    ):
        return _result("missing_output", "代码执行结束但没有生成声明的输出文件。", False)
    if "invalid_expected_output" in text or "预期输出未通过运行时验证" in message:
        return _result("invalid_output", "代码生成了文件，但文件未通过格式或内容验证。", False)
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
    if "invalid join field" in text and "does not exist" in text:
        return _result(
            "field_not_found",
            "属性连接引用了中间图层中不存在的字段；分组聚合可能没有显式输出连接键。",
            False,
        )
    if "group_by 不会自动写入输出字段" in text:
        return _result(
            "generated_code_api",
            "生成代码错误地假定 native:aggregate 会自动输出 GROUP_BY 分组字段。",
            False,
        )
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
    if "projwin" in text and (
        "参数值错误" in message
        or "invalid value" in text
        or "incorrect parameter value" in text
    ):
        return _result(
            "processing_parameters",
            "gdal:cliprasterbyextent 的公开 Processing 范围参数名就是 PROJWIN；"
            "此错误通常表示代码误写为 EXTENT、遗漏了 PROJWIN，或传入的范围值无效。"
            "应给 PROJWIN 传 QgsMapLayer、QgsRectangle 或 xmin,xmax,ymin,ymax 字符串。",
            False,
        )
    if "isgeosempty" in text:
        return _result("generated_code_api", "生成代码调用了不存在的 PyQGIS API。", False)
    if "qgsprocessingcontext" in text and "invalidgeometrycheck" in text:
        return _result(
            "generated_code_api",
            "生成代码把 Qgis.InvalidGeometryCheck 枚举错误地写在 QgsProcessingContext 下。",
            False,
        )
    if "qeventloop" in text and "allevents" in text and "has no attribute" in text:
        return _result(
            "executor_qt_compatibility",
            "执行器使用了 Qt5 的事件循环枚举路径，但当前 QGIS 运行在 Qt6。",
            False,
        )
    if (
        "qgssinglebandpseudocolorrenderer" in text
        and "qgscolorrampshader" in text
        and "unexpected type" in text
    ):
        return _result(
            "generated_code_api",
            "生成代码把 QgsColorRampShader 直接传给了需要 QgsRasterShader 的伪彩色渲染器。",
            False,
        )
    if "syntaxerror" in text:
        return _result("syntax_error", "生成的 Python 代码存在语法错误。", False)
    if "nameerror" in text or "is not defined" in text:
        return _result("name_error", "生成代码使用了未定义或未导入的名称。", False)
    if "qgsprocessingexception" in text or "processingexception" in text:
        return _result("processing_error", "QGIS Processing 算法执行失败。", False)
    return _result("unknown", "尚未匹配到已知错误类别，需要人工归因。", False)


def _result(error_code: str, cause: str, retryable: bool) -> dict[str, object]:
    return {"error_code": error_code, "cause": cause, "retryable": retryable}
