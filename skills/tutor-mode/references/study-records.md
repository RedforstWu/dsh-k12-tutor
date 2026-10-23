# 学习记录工具与管理操作

学习记录默认关闭。只为批准的工作区安装配置。安装命令会修改指定 K12 profile，需要按现有发布流程单独授权和激活。

```powershell
& $PY tools/install_profile.py --check --study-workspace 'C:\approved\student'
& $PY tools/install_profile.py --apply --study-workspace 'C:\approved\student'
```

`--study-workspace` 可以重复传入。插件需要 `studyEnabled: true` 和精确匹配的 `studyWorkspaces` 路径。其他工作区不捕获、不调用学习记录工具。工作区账本中的观察集成开关持久保存；已经启用的工作区不能绕回 `learner_profile(observe)` 重复记账。

管理绑定使用 `tools/study_binding.py bind --workspace <绝对路径> --config <配置文件>`。配置包含 `studentId`、HTTPS `endpoint`、环境变量名 `credentialRef` 和固定 CLI 启动数组 `command`。Windows 使用绝对 node.exe 与绝对 bin/k12.js 路径。配置不能放令牌值。令牌只从命名环境变量读取并放入独立 CLI 子进程环境。绑定核验 whoami 学生身份及服务端 `learning-v1` 能力，并固定 CLI 文件哈希。相同学生令牌轮换重新 bind。另一个学生或站点必须使用新的工作区。复制账本不会继承授权。未绑定时产生的记录不会在补绑定后自动上传。

绑定状态可用 `tools/study_binding.py status --workspace <绝对路径>` 查看。绑定数据放在 K12 profile 的外部 SQLite 中，不放学生工作区。模型没有管理绑定入口。学习记录工具、查询和每轮停止时运行有界同步；DSH 关闭后不承诺后台上传。

## 对话工具

独立评估直接调用 `study_assessment({assessment:{templateId,params},slot,purpose})`，不先 read、route_task 或输出开场白。引擎内部固定路由，工具只返回 exerciseId、answerSlots、deliveryText；原样单独发送 deliveryText 后等待真实回答。原生宿主工具参数和结果可展开查看，因此其他工具均不能作为独立评估的中性事件。

普通辅导先 `route_task(action=start)`，再在学生回答前调用 `study_record(action=open)`。taskRef 使用当前任务 ID 或 `sessionId:taskId`。预定义题目槽为 `diagnose-1`、`practice-1`、`transfer-1`、`review-1`、`reflection-1`。同任务同槽重试复用练习 ID，题目变化须开始新任务。question 包含 `type=single|multiple|blank|free`、stem、options 和 referenceAnswer。选择题 options 为 `{key,text}` 数组，其余题型为 null；未知参考答案为 null。

open 返回 `exerciseId` 和答案槽 `whole`、`reflection`、`step-1`。插件注入真实输入的 responseRef，不接受模型提交正文、学生 ID、时间、凭据或新 eventId。answer 使用 exerciseId、responseRef、answerSlot 和对应 scope。`whole` 对应 `{kind:'whole'}`，`reflection` 对应 `{kind:'reflection'}`，`step-1` 对应 `{kind:'step',stepKey:'step-1'}`。

判定为 `{state:'graded',correct:true,errorCause:null}` 或 `{state:'graded',correct:false,errorCause:'concept'}`。错因可为 concept、method、calc、misread、careless 或 null。未确认判定用 `{state:'pending',reason:'technical'|'incomplete'|'needs_review'}`。宿主没有可靠序号或时间时保留 null，证据不完整只能 pending。早于练习打开的上下文消息不能充作该练习作答。

受控评估可用 assessment 替代 question：`{templateId:"linear-function-value/v1",params:{k:2,b:1,x:2}}`、`linear-equation/v1` 的 `{a,b,c}`、`linear-inequality/v1` 的 `{a,b,c,op:"<"}`（op 也可为 >）。整数范围 -20..20，a/k 非零，模板必须匹配当前路由知识点。open 返回 deliveryText，必须原样单独发给学生，不加解释、提示、页面或其他文案，再等待真实回答。自由 question 继续支持，但帮助未知，不取得独立资格。宿主完整事件窗口确认仅输出该题、没有其他输出且首次整题作答时，才生成独立性证明；reportedAssistance 只能降级到 hint/revealed，不能证明独立。同题或等价方程重复不能恢复首答资格。`skip` 需要真实输入中的明确跳过或结束表达与宿主时间。`dismiss` 仅标记自述、上下文、非作答或技术问题，不生成作答。

`revise` 使用 eventId、当前 localRevision、新 judgment 和 reason。远端规范版本由引擎保存，不允许模型填入。发生 conflict 后先查询远端当前状态，再作新修订决定；原意图不会自动更换远端版本后覆盖。

`study_query(kind=recent|wrong|mastery|gaps,limit=1..100)` 返回 live、cached 或 unavailable，带 observedAt 和同步状态。unavailable 不等于没有历史。参考答案和解析不进入默认远端历史。recent 和 wrong 支持 cursor、knowledgeId、exerciseId 过滤与续页。受控模板使用版本化映射 tutor-math/v1；revise 可提供 mappingVersion 与 reason，由引擎从冻结模板派生映射，不能填写知识点 ID。旧快照不会随映射升级静默变化。缺少映射的记录可以保存，不能用于 tutor 观测。宿主后发现窗口缺失或输出污染时追加帮助降级修订，保留原上传载荷与重放身份。


独立性范围仅为宿主可审计的受控模板窗口。同会话先前未归属的解释、提示、反馈、页面或未知输出会持续使后题为 unknown；打开未完成的占位模板不能清除历史。只有已闭合练习中的精确模板题面可作为已知历史。跨会话只排除账本已经记录的相同模板/等价参数题，不能证明任意旧自由题或外部教学没有泄露答案。后补污染的安全降级如遇远端版本变化，会基于刚读到的版本追加新 help-only mutation，不改变旧重放字节，也不覆盖家长判定或映射。


DSH 的 reasoning 内容可展开查看，不能按“内部推理”从帮助证据中忽略。使用带 reasoning 输出的模型时，该窗口保持 unknown；独立评估的宿主验收需要实际不输出 reasoning 的模型设置，并且首次 open 前也不发送开场白。
