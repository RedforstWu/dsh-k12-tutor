# 学习记录工具与管理操作

本地题目账本在辅导工作区捕获真实输入，独立于远端学习记录开关。远端同步默认关闭，只为批准的工作区配置。以下安装命令修改指定 K12 profile，需要按现有发布流程单独授权和激活。

```powershell
& $PY tools/install_profile.py --check --study-workspace 'C:\approved\student'
& $PY tools/install_profile.py --apply --study-workspace 'C:\approved\student'
```

`--study-workspace` 可以重复传入。legacy 远端工具准入需要 `studyEnabled: true` 和精确匹配的 `studyWorkspaces` 路径；本地捕获和 study_record 不因此启用绑定、上传或旧远端入口。家庭模式使用宿主已验证的学习空间。作答事实在所有模式都只走题目账本，不另写 learner_profile observe。

管理绑定使用 `tools/study_binding.py bind --workspace <绝对路径> --config <配置文件>`。配置包含 `studentId`、HTTPS `endpoint`、环境变量名 `credentialRef` 和固定 CLI 启动数组 `command`。Windows 使用绝对 node.exe 与绝对 bin/k12.js 路径。配置不能放令牌值。令牌只从命名环境变量读取并放入独立 CLI 子进程环境。绑定核验 whoami 学生身份及服务端 `learning-v1` 能力，并固定 CLI 文件哈希。相同学生令牌轮换重新 bind。另一个学生或站点必须使用新的工作区。复制账本不会继承授权。未绑定时产生的记录不会在补绑定后自动上传。

绑定状态可用 `tools/study_binding.py status --workspace <绝对路径>` 查看。绑定数据放在 K12 profile 的外部 SQLite 中，不放学生工作区。模型没有管理绑定入口。学习记录工具、查询和每轮停止时运行有界同步；DSH 关闭后不承诺后台上传。

## 对话工具

独立评估直接调用 `study_assessment({assessment:{templateId,params},slot,purpose})`，不先 read、route_task 或输出开场白。引擎内部固定路由，工具只返回 exerciseId、answerSlots、deliveryText；原样单独发送 deliveryText 后等待真实回答。原生宿主工具参数和结果可展开查看，因此其他工具均不能作为独立评估的中性事件。

普通辅导需要持续目标时 `route_task(action=start)`。题目开立必须早于可记录作答，不能为已有解答补造独立练习。同目标的新题直接 `study_record(action=open)`，不用再 start。taskRef 使用返回的 `sessionId:taskId` 或本会话任务 ID。slot 为 diagnose、practice、transfer、review、reflection 加 `-1` 至 `-9999`，如 practice-2；purpose 仍使用工具规定的有限集合。相同任务与槽位重试复用身份，同槽改快照报冲突，新题使用新槽位。可用 follows 关联原题，不清除原帮助。

question 包含 `type=single|multiple|blank|free`、stem、options 和 referenceAnswer。选择题 options 为 `{key,text}` 数组，其余题型为 null；未知参考答案为 null。只改教学方法、图示或表达不改变题意时，保留 exerciseId，用 adapt 调整指导。新条件改变答案时开新题。回到旧任务或题目用 resume；open 重试不会抢回焦点。返回 navigation_failed 时使用回执中的 taskRef/exerciseId 显式恢复，不再发一遍新题。

open 返回 exerciseId 和答案槽说明。插件注入真实输入的 responseRef，不接受模型提交正文、学生 ID、时间、凭据或新 eventId。answer 使用 exerciseId、responseRef、answerSlot 和对应 scope。whole 对应 `{kind:'whole'}`，reflection 对应 `{kind:'reflection'}`，step-N 对应 `{kind:'step',stepKey:'step-N'}`，N 为 1..9999。同一步真实重答使用新 responseRef；修正既有判定用 revise，不能把同一引用重复制造成新尝试。

判定为 `{state:'graded',correct:true,errorCause:null}` 或 `{state:'graded',correct:false,errorCause:'concept'}`。错因可为 concept、method、calc、misread、careless 或 null。未确认判定用 `{state:'pending',reason:'technical'|'incomplete'|'needs_review'}`。宿主没有可靠序号或时间时保留 null，证据不完整只能 pending。早于练习打开的上下文消息不能充作该练习作答。

受控独立评估使用专用 study_assessment 入口，不用普通 open 代替。模板及参数以当前工具 Schema 为准。必须将返回的 deliveryText 原样单独发给学生，不加解释、提示、页面或其他文案，再等待真实回答。自由 question 的帮助未知，不取得独立资格。宿主完整事件窗口确认仅输出该题、没有其他输出且首次整题作答时，才生成独立性证明；reportedAssistance 只能降级到 hint/revealed，不能证明独立。同题或等价方程重复不能恢复首答资格。skip 需要真实输入中的明确跳过或结束表达与宿主时间。dismiss 仅标记自述、上下文、非作答或技术问题，不生成作答。

`revise` 使用 eventId、当前 localRevision、新 judgment 和 reason。远端规范版本由引擎保存，不允许模型填入。发生 conflict 后先查询远端当前状态，再作新修订决定；原意图不会自动更换远端版本后覆盖。

`study_query(kind=recent|wrong|mastery|gaps,limit=1..100)` 返回 live、cached 或 unavailable，带 observedAt 和同步状态。unavailable 不等于没有历史。参考答案和解析不进入默认远端历史。recent 和 wrong 支持 cursor、knowledgeId、exerciseId 过滤与续页。受控模板使用版本化映射 tutor-math/v1；revise 可提供 mappingVersion 与 reason，由引擎从冻结模板派生映射，不能填写知识点 ID。旧快照不会随映射升级静默变化。缺少映射的记录可以保存，不能用于 tutor 观测。宿主后发现窗口缺失或输出污染时追加帮助降级修订，保留原上传载荷与重放身份。


独立性范围仅为宿主可审计的受控模板窗口。同会话先前未归属的解释、提示、反馈、页面或未知输出会持续使后题为 unknown；打开未完成的占位模板不能清除历史。只有已闭合练习中的精确模板题面可作为已知历史。跨会话只排除账本已经记录的相同模板/等价参数题，不能证明任意旧自由题或外部教学没有泄露答案。后补污染的安全降级如遇远端版本变化，会基于刚读到的版本追加新 help-only mutation，不改变旧重放字节，也不覆盖家长判定或映射。


DSH 的 reasoning 内容可展开查看，不能按“内部推理”从帮助证据中忽略。使用带 reasoning 输出的模型时，该窗口保持 unknown；独立评估的宿主验收需要实际不输出 reasoning 的模型设置，并且首次 open 前也不发送开场白。
