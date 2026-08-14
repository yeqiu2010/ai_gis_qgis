---
name: sam3-remote-segmentation
lifecycle: task
description: 通过已配置的 SAM3-Geo-API 从 QGIS 遥感、卫星或航空影像中分割植被、林地、农田、建筑、水体、道路、运动场、屋顶、树冠等可见地物或区域，支持文本提示、自动分割、AOI 范围和选中矢量要素边界框提示，输出地理配准掩码或面图层。适用于影像地物识别与提取、遥感影像分割，以及必须先分割再继续裁剪、相交、空间连接、面积/占比统计或制图的任务。
allowed-tools:
  - list_layers
  - inspect_layer
  - inspect_layers
  - check_sam3_service
  - inspect_sam3_segmentation_inputs
  - segment_remote_sensing_image
  - create_plan
  - revise_plan
  - get_task_state
  - update_plan_step
  - complete_plan_step
  - register_artifact
  - finalize_task
  - load_skill
license: MIT
metadata:
  hermes:
    tags: [sam3, remote-sensing, segmentation, vegetation, raster, vectorization]
    related_skills: [qgis-toolbox, gis-pipeline, generate-land-cover-map]
    requires_tools: [check_sam3_service, inspect_sam3_segmentation_inputs, segment_remote_sensing_image]
  qgis_agent:
    inputs:
      required:
        input_layer_id: {type: qgis_raster_layer}
      optional:
        prompt: {type: string}
        aoi_layer_id: {type: qgis_vector_layer}
        boxes_layer_id: {type: qgis_vector_layer}
    outputs:
      segmentation_outputs: {type: array}
    side_effects:
      uploads_data: true
      modifies_qgis_project: true
      writes_files: true
      requires_confirmation: true
      concurrency: qgis_main_thread_serial
    completion:
      required_artifacts: [segmentation_outputs]
      checks: [output_files_exist, output_layers_loaded]
---

# SAM3 遥感影像分割

通过固定受信任工具完成影像预处理、SAM3 API 调用、掩码校验、矢量化和 QGIS 图层加载。不要生成网络访问代码，不要用 `execute_gis_code` 调用 SAM3。

## 路由模式

- 用户要求从遥感、卫星或航空影像中“提取/识别”植被区域或其他可见地物时，属于本 Skill 的影像分割语义；不能因为目标是植被就改走 NDVI、波段计算或通用 `gis-pipeline`。
- 只有用户明确要求 NDVI、光谱指数、监督/非监督分类等光谱方法时，才不使用 SAM3。SAM3 服务不可用时应报告失败并询问用户是否接受替代方法，不能静默改用 NDVI。
- 用户描述目标类别时使用 `mode=text`，把类别规范化为简短英文 prompt，同时保留用户原始语义。
- 用户明确要求自动识别全部可见对象时使用 `mode=automatic`。说明该模式是实验性的通用提示分割。
- 用户指定候选框或选中要素时使用 `mode=boxes`，传入 `boxes_layer_id`；默认只使用选择集。
- 用户指定行政区、地块或样区作为处理范围时使用 `scope_mode=aoi` 和 `aoi_layer_id`。
- 用户用道路周边距离、点位服务范围等方式描述处理范围时，如果现有矢量还不是面 AOI，先按任务计划调用 `qgis-toolbox` 生成面缓冲区；确认成功后必须返回本 Skill，并把缓冲结果 `loaded_layers[].id` 传为 `aoi_layer_id`。
- 用户说“当前视图/当前地图范围”时使用 `scope_mode=canvas`。
- 其余情况使用 `scope_mode=full`；输入检查提示超限时不得继续全图上传。

## 强制流程

1. 调用 `list_layers`，以真实 layer ID 绑定输入影像和可选矢量图层。
2. 必要时调用 `inspect_layer`/`inspect_layers` 消除同名、图层类型、CRS 或选择范围歧义。
3. 调用 `check_sam3_service`。模型未就绪、服务不可达或鉴权失败时停止。
4. 调用 `inspect_sam3_segmentation_inputs`，传入最终影像、范围、波段和框图层参数。
5. 检查 `inspection_complete`、预计像元数、上传大小、RGB 波段、空间相交和 warnings。`success=false` 时不得执行。
6. 调用 `segment_remote_sensing_image`。该工具需要用户确认，并会明确上传目标服务、写入文件和加载图层。
7. 输入检查成功后必须在同一 Agent turn 直接调用 `segment_remote_sensing_image` 来触发插件正式确认。不得把内部工具结果原样回复给用户，不得先用自然语言询问“是否确认”，也不得要求用户输入“继续”或“确认执行”；插件的 `confirm_request` 是唯一确认入口。
8. 存在 `create_plan` 建立的活动任务时，工具成功后使用 `register_artifact` 登记 `artifact_type=segmentation_outputs`，value 至少包含 `job_id`、`outputs` 和 `loaded_layers`。没有活动任务时不要调用 `register_artifact`；AgentCore 已保留分割结果，不能制造“当前会话没有活动任务”的无意义失败。
9. 只有存在真实输出文件和已加载图层时才报告分割完成。

## 参数规则

- `input_layer_id`、`aoi_layer_id`、`boxes_layer_id` 必须使用工具返回的真实 ID，不使用名称猜测。
- 文本 prompt 优先使用单个英文类别或短语，例如 `building`、`water`、`road`、`tree canopy`、`solar panel`。
- 用户一次指定多个置信度阈值时，每个阈值是一次独立分割。必须先调用 `create_plan`，为每个阈值建立一个独立且可辨识的步骤，例如 `segment_0_5`、`segment_0_3`，并在步骤输入中保存 `confidence_threshold`。按用户给定顺序各执行一次，不能把所有阈值合并成一个步骤。
- 多阈值输出必须使用不同 `output_name`，例如 `building_threshold_0_5` 和 `building_threshold_0_3`；每次成功后以工具结果中的 `parameters.confidence_threshold`、`job_id`、`outputs` 和 `loaded_layers` 判断该阈值已经完成，再推进下一阈值。
- 同一用户请求中，相同 `input_layer_id`、范围、prompt、`confidence_threshold` 和输出参数的分割成功后严禁重复调用。若工具返回 `duplicate_prevented=true` 或 `already_completed=true`，必须复用已有产物并处理下一未完成阈值，不得再次请求确认。
- 不确定用户目标类别时先询问，不能静默扩大或改变类别。
- `min_size_pixels`/`max_size_pixels` 的单位是像素数，不是平方米。
- 用户未指定输出时默认 `output_types=["vector"]`；要求保留掩码时使用 `["vector", "raster"]`。
- 多波段影像只有在用户明确指定时传 `rgb_bands`；否则让检查工具选择前三波段或复制灰度波段。
- `output_name` 使用简短类别名，不传路径。文件由工具写入隔离工作目录。
- `zoom_to_result` 默认保持开启时，缩放只能改变范围，必须保留用户原有地图画布旋转角；不得旋转输入影像或地图画布。用户明确要求保持当前视图时传 `false`。
- 不允许向工具传服务 URL、token、Python 代码或外部输出路径。

## 矢量组合流程

用户请求包含分割后的 GIS 操作时，先完成并登记分割产物，再加载后续 Skill：

- 一个明确单步操作，例如裁剪或缓冲：加载 `qgis-toolbox`。
- 多图层、多步骤、统计汇总、字段/CRS 推断或制图：加载 `gis-pipeline`。

后续流程必须使用 `segment_remote_sensing_image.loaded_layers[].id` 作为分割图层输入。不要假设输出图层名称，也不要在分割成功前生成后续代码。

对于“从 satellite 提取植被区域并统计植被面积占影像面积的比例”这类复合请求，固定顺序是：先在任务计划中建立 SAM3 分割步骤及其依赖的统计步骤；完成 SAM3 服务检查、输入检查、分割确认和 `segmentation_outputs` 登记；然后才加载 `gis-pipeline` 计算面积和比例。禁止用 NDVI 掩膜替代计划中的 SAM3 分割步骤。

AOI 模式的矢量输出已经按 AOI 精确裁剪。若用户还要求按每个地块统计，后续 Pipeline 再用结果面与原地块做相交、面积汇总和比例计算。

前置 AOI 生成不属于“分割后的 GIS 操作”：当用户要求在某条道路周边范围内分割时，允许计划先使用 `qgis-toolbox` 生成道路缓冲面，再执行本 Skill。缓冲区只是 `aoi_layer_id` 输入，不能作为任务最终结果；其确认执行结束后必须继续 SAM3 服务检查、输入检查和分割。

## 完成与失败

- `no_objects_found` 是没有匹配对象，不是成功。建议调整 prompt、置信度或范围。
- `input_limit_exceeded` 时改用 AOI/当前画布，不能绕过像元或框数量门禁。
- 矢量化失败但 raster 输出成功时，明确报告部分成功；只有用户接受掩码时才可继续。
- API 超时后不要自动重复提交，避免服务端仍在推理时创建重复任务。
- 回复中说明实际模式、prompt、对象数、输出图层名、文件路径和 warnings。
