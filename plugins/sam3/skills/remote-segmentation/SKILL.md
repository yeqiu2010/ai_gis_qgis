---
name: remote-segmentation
lifecycle: task
description: 使用 SAM3 对 QGIS 遥感栅格进行文本、自动或边界框提示分割，并把结果作为后续矢量分析的真实输入。
tools:
  - check_sam3_service
  - inspect_sam3_segmentation_inputs
  - segment_remote_sensing_image
  - load_skill
  - create_plan
  - get_task_state
  - update_plan_step
  - finalize_task
metadata:
  version: "1.0.0"
  hermes:
    requires_tools:
      - check_sam3_service
      - inspect_sam3_segmentation_inputs
      - segment_remote_sensing_image
  qgis_agent:
    outputs:
      segmentation_layer: SAM3 生成的栅格或矢量图层
    side_effects:
      writes_project: true
---

# SAM3 遥感影像分割

当用户要求从遥感栅格中识别、提取或分割地物时，必须使用本 Skill。不要因为目标可用光谱指数近似，就擅自改用 NDVI、阈值分类或生成代码。

执行顺序：

1. 读取真实图层 ID；必要时检查服务状态。
2. 调用 `inspect_sam3_segmentation_inputs` 验证范围、波段和 AOI。
3. 检查成功后直接调用 `segment_remote_sensing_image`。确认只能由工具 checkpoint 发出，不要用自然语言先问一次。
4. 工具返回的 `loaded_layers` 和 `artifacts` 是后续统计、相交、裁剪、平滑或导出的真实输入。
5. 若用户还要求后处理，继续加载对应 Skill 并完成整个计划，不能在分割完成时提前结束。

如果前序矢量处理生成了道路缓冲区或其他 AOI，道路缓冲区应作为后续 SAM3 的 `aoi_layer_id`，并使用 `scope_mode=aoi`；缓冲区只是中间 Artifact，不是最终分割结果。

每个不同阈值是一组独立参数；相同参数不得重复提交。输出后不得修改输入影像的 CRS、旋转角或画布旋转。只有用户明确要求时才缩放到结果。
