---
id: diagnose
name: 错因诊断
when: 学生带着做错的题、自己的解题过程或老师的批改，问错在哪
principles: [errors-as-data, compute-then-explain, diagnose-first, check-understanding]
status: full
---

目标：找到有证据支持的错误位置或理解缺口，确定下一步帮助。

先看学生已有过程，缺少关键一步时只补问那一处。用 math_check 核对标准解与相邻变形，找到第一处不成立的步骤。图片或识别不完整先修输入，不把技术问题当知识错误。

可以让学生检查该步、比较正反例、说明使用条件，或用更简单的表征暴露关系。证据不足时保留假设，不凭一次失误认定“粗心”或固定误区。连续不能回应同一诊断问题时，换材料或先补前置。

观察学生是否找到关键条件、能否自己改动原步骤。卡点已清楚就 adapt 到 solve 或 concept，别为了诊断继续追问。可邀请一个有针对性的新动作检验是否迁移，也允许结束。

作答只记录一次。关于误区的解释用带 evidenceRefs 的 observation，说明 fact 或 hypothesis；一次改对不自动写 resolved，也不生成正式掌握结论。
