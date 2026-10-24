# 互动网页

由 tools/gen_activity_catalog.py 生成，不要手改。

先确定当前需要学生做什么，再选最简单的媒介。对话或已有白板足够时不生成页面，没有独立新用途就继续原材料。

| 学生动作 | 可选媒介 |
| --- | --- |
| 观察一个量的变化及对应关系 | 简短对话、现有白板；需要自己操作时选适用模板 |
| 比较条件、找差异或反例 | 两段短材料或白板；需要选择与反馈时可用方案组件 |
| 观察同类示例后补一处 | 对话或白板；确需分步播放时可用 stepwise |
| 回忆、解释或应用 | 对话通常足够；交互输入有独立用途时可用方案组件 |

决定做网页后，先复用适合的模板或方案组件。确有新用途且现有能力不适合时才写 custom；没有模板不意味着必须做页面。
用 `make_activity` 填简短 purpose，说明学生的具体动作。taskRef 和 exerciseId 由宿主关联创建时的当前焦点，没有题目时 exerciseId 为 null，模型不填写身份。
再用 `present` 发给学生（路径用 make_activity 返回的 path）。学生点开对话里的卡片，页面在右侧打开。
页面不能把结果传回给你：请学生做完回到对话里说结果，你再按 playbook 继续。

答案策略照常生效：引擎会拒绝把 withhold 任务的答案放进页面，
作业原题也不能做成会显示结果的页面，这时换一道变式题。考试中和越界任务不做互动页。

## 模板（kind=template）

### `resource-comparison` 两站资源比较

先预测，再调节时间观察两站资源，自己写等量关系；回到对话验算并做无主题迁移题。

params 例：

```json
{}
```

### `number-line` 数轴上走一走

有理数加减：学生先猜结果，再看在数轴上一步步走。

params 例：

```json
{
 "expression": "-6+(-8)"
}
```

### `balance` 等式天平

一元一次方程：学生自己选择两边同时做的操作，体会移项来自等式性质。

params 例：

```json
{
 "equation": "3x+20=4x-25"
}
```

### `kb-sliders` k、b 滑块

一次函数：拖动 k、b 完成任务，看图象怎样变化。

params 例：

```json
{
 "tasks": [
  {
   "text": "让直线从左往右下降，并经过点 (0, 3)",
   "k_sign": "neg",
   "b": 3
  }
 ]
}
```

### `zero-product` 找全方程的根

一元二次方程：学生填根、代回检验，图象显示是否找全（针对两边除以含未知数的式子丢根）。

params 例：

```json
{
 "equation": "x(x-2)+x-2=0"
}
```

### `inequality-line` 不等式数轴

一元一次不等式：学生代数试验、在数轴上画解集（针对乘除负数不变号）。

params 例：

```json
{
 "inequality": "-2x+3>7"
}
```

### `substitution-table` 代入检验表

列代数式：用具体数检验学生写的式子是否合理（针对字母当对象的反转错误）。

params 例：

```json
{
 "story": "某校学生人数是老师人数的 6 倍",
 "student_equation": "6S=T",
 "correct_equation": "S=6T",
 "given": "T",
 "find": "S",
 "given_label": "老师人数",
 "find_label": "学生人数"
}
```

### `stepwise` 分步讲解动画

任何知识点：逐步展示推导，每步一句讲解；相邻两步的式子由引擎验算等价。展示完整过程，只用于变式题或已揭晓的题。

params 例：

```json
{
 "title": "移项为什么变号",
 "steps": [
  {
   "say": "原方程。",
   "show": "3x+20=4x-25"
  },
  {
   "say": "两边同时减去 [[4x]]。",
   "show": "3x-4x+20=-25"
  },
  {
   "say": "两边同时减去 [[20]]。",
   "show": "3x-4x=-25-20"
  }
 ]
}
```

`stepwise` 的 steps：每步 `say`（这一步讲什么）和可选的 `show`（这一步显示的式子，用 `[[ ]]` 标出要高亮的部分）。
相邻两步的式子必须等价，引擎会验算；某些步骤只放文字时设 `check: false`。可选 `ending`（最后一步后的提示）。

## 方案库组件（kind=scheme，需要联网）

加载互动方案库的线上组件，`params` 必须符合该方案的参数格式；`style` 可选 cartoon、retro-industrial、urban-tech。

### `choice-text.text-multi` 文本多选（文本选项）（0.3.0）

玩家阅读一道文字题干，从 2～4 个文字选项中勾选全部正确项后提交；可配置倒计时，归零时自动锁定作答。判定后显示正确答案与解析，玩家点击“继续”时上报正确、错误或超时。

  - `title` string：弹窗顶部标题，选填；不传时显示“请完成多选题”。
  - `timer` ['integer', 'string']：作答限时，选填。整数秒（如 60），或字符串 "60"、"60s"、"01:00"（分:秒）；必须大于 0。不需要限时时省略。
  - `question`（必填） string：题目内容，只承载一道题，须体现多选语义。
  - `options`（必填） array：文本选项，按数组顺序展示。每项为字符串（如 "A. 选项文案"）或对象 { label, text }，推荐对象格式。
  - `answer`（必填） string：全部正确选项的字母标识，用英文逗号分隔，至少 2 个，如 "A,C"；判定后原样显示在答案区。
  - `explanation` string：答案解析，选填；判定后无论结果都会显示，无解析时可留空字符串。
  - `background` string：全屏背景图地址，选填，显示在半透明遮罩下方。

例：

```json
{
 "title": "请完成多选题",
 "timer": "60s",
 "question": "下列句子中，使用了“拟人”修辞手法的选项是",
 "options": [
  {
   "label": "A",
   "text": "蝴蝶在花丛中跳舞，像一位优雅的舞者。"
  },
  {
   "label": "B",
   "text": "春天来了，小草偷偷地从土里探出头来。"
  },
  {
   "label": "C",
   "text": "风儿轻轻抚摸着我的脸，像妈妈的手一样温柔。"
  },
  {
   "label": "D",
   "text": "月亮静静地挂在夜空，注视着大地。"
  }
 ],
 "answer": "B,D",
 "explanation": "拟人是把事物当作人来写，让它有人类的动作、情感或语言。B和D都符合这个特点。",
 "background": "https://t8.baidu.com/it/u=1463230774,1776177532&fm=193"
}
```

### `choice-text.text-single` 文本单选（文本选项）（0.3.0）

玩家阅读一道文字题干，从 2～4 个文字选项中点选唯一最佳答案；点选 0.5 秒后判定并显示正确答案与解析，玩家点击“继续”时上报正误。

  - `title` string：弹窗顶部标题，选填；传入时不能为空白。
  - `question`（必填） string：题目内容，只承载一道题，清晰描述题目情境和答题要求。
  - `options`（必填） array：文本选项，按数组顺序展示。每项为字符串（如 "A. 选项文案"）或对象 { label, text }，推荐对象格式。
  - `answer`（必填） string：正确选项的字母标识，单个大写字母，必须与某个选项的标识一致。
  - `explanation` string：答案解析，选填；判定后无论对错都会显示，无解析时可留空字符串。

例：

```json
{
 "title": "选择最恰当的一项",
 "question": "虽然老师在课堂上反复强调了审题的重要性，并举了许多因粗心大意而失分的例子，但小明在完成数学试卷时，依然_______地将一道简单的计算题中的‘除’看成了‘除以’，导致整道大题一分未得。事后他非常后悔，意识到自己必须改掉这个坏毛病。",
 "options": [
  "A. 一如既往",
  "B. 无独有偶",
  "C. 明知故犯",
  "D. 重蹈覆辙"
 ],
 "answer": "C",
 "explanation": "句中强调“老师提醒过 + 小明依然犯错”这一明知道理却故意（或放任自己）犯错的情景，因此明知故犯最恰当。"
}
```

### `fill-drag` 拖拽填空（0.3.0）

题干中用【正确词】标出 1～5 个挖空，玩家从正答与干扰项混合打乱的词库中点选或拖拽词条填入，填满后提交一次，逐空显示对错。

  - `title` string：弹窗顶部标题，选填；传入时不能为空白。
  - `question`（必填） string：题干。用【正确词】标出 1～5 个挖空，括号内文字即该空的正答（首尾空白会被去掉）；括号外不得再出现【或】。
  - `options` array：干扰项列表，选填。组件把题干中的全部正答与 options 合并去重后随机打乱，作为词库展示。

例：

```json
{
 "title": "生物：生态系统成分",
 "question": "在生态系统中，【栖息地】为生物提供生存环境，【生产者】是能量流动的起点，【分解者】促进物质循环，【消费者】维持物种平衡，【生物多样性】则是生态稳定的重要标志。",
 "options": [
  "非生物环境",
  "捕食者",
  "初级生产量",
  "食物网"
 ]
}
```

### `fill-keyboard` 键盘填空（0.3.0）

题干中用【正确值】标出 1～4 个数字或算式挖空，玩家点击空位后用数字、小数点或运算符虚拟键盘输入，提交一次后逐空显示对错与正确答案。

  - `title` string：弹窗顶部标题，选填；传入时不能为空白。
  - `question`（必填） string：题干。用【正确值】标出 1～4 个挖空，括号内即该空唯一的正确输入，只能包含所选键盘能键入的字符；括号外不得再出现【或】。可在首行写操作提示，与正文用换行分隔。
  - `keyboard` enum numeric/numeric-point/operators：虚拟键盘类型，选填，缺省为 numeric。numeric：数字 0～9；numeric-point：数字与小数点；operators：数字、小数点与 + - × ÷。

例：

```json
{
 "question": "请填入正确答案\n一个长方体容器，从里面量长20厘米，宽15厘米，里面装有水，水深8厘米。现在将一个体积为900立方厘米的铁块完全浸没在水中（水未溢出）。此时，水面会上升【3】厘米，上升后的水深为【11】厘米，容器内水的体积是【2400】立方厘米。",
 "keyboard": "numeric"
}
```

### `judge-button` 按钮判断（0.3.0）

弹窗逐题展示文字或图片陈述，玩家点击“错误”或“正确”按钮作答，每题显示对错与正确答案，全部答完按全部答对、全部答错、部分答对给出结果。

  - `title` string：弹窗标题，选填；显示在题干上方。
  - `question`（必填） string：判断题组的题干正文，必须含非空白字符。
  - `completionDelayMs` number：历史兼容字段，选填。组件只校验它是非负数，不使用它；完成后由玩家点击“继续”结束。
  - `questions`（必填） array：判断题，按数组顺序逐题展示。

例：

```json
{
 "title": "判断练习",
 "question": "请判断以下说法是否正确",
 "completionDelayMs": 2000,
 "questions": [
  {
   "text": "橡皮泥挤压发生形变",
   "img": "",
   "answer": "错误"
  },
  {
   "text": "用手按弹簧，弹簧产生形变的力就是弹力",
   "img": "https://img0.baidu.com/it/u=3097612138,58988988&fm=253&fmt=auto&app=138&f=JPEG?w=400&h=400",
   "answer": "正确"
  },
  {
   "text": "",
   "img": "https://t10.baidu.com/it/u=1926281935,263506505&fm=30&app=106&f=JPEG?w=640&h=839&s=79843D76455362645E7C166E0200A074",
   "answer": "正确"
  }
 ]
}
```

### `judge-swipe` 滑动判断（0.3.0）

玩家逐张查看文字或图片判断卡片，左滑表示错误、右滑表示正确（也可点击两侧“错误”“正确”标识），每张即时反馈对错，全部答完按全部答对、全部答错、部分答对给出结果。

  - `title` string：页面标题，选填；显示在题干上方。
  - `question`（必填） string：判断题组的题干，显示在页面顶部。
  - `completionDelayMs` number：历史兼容字段，选填。组件只校验它是非负数，不使用它；完成后由玩家点击“继续”结束。
  - `questions`（必填） array：判断卡片，按数组顺序逐张展示。

例：

```json
{
 "title": "判断练习",
 "question": "请做出对错判断",
 "completionDelayMs": 2000,
 "questions": [
  {
   "text": "橡皮泥挤压发生形变",
   "img": null,
   "answer": "错误"
  },
  {
   "text": "用手按弹簧，弹簧产生形变的力就是弹力",
   "img": "https://img0.baidu.com/it/u=3097612138,58988988&fm=253&fmt=auto&app=138&f=JPEG?w=400&h=400",
   "answer": "正确"
  },
  {
   "text": "",
   "img": "https://t10.baidu.com/it/u=1926281935,263506505&fm=30&app=106&f=JPEG?w=640&h=839&s=79843D76455362645E7C166E0200A074",
   "answer": "正确"
  }
 ]
}
```

### `match-line` 连线配对（0.4.0）

A、B 两侧各 2～8 项文字或图片，打乱顺序展示；玩家拖动或先后点击两侧选项建立一对一连线，全部连上后提交统一判定，结果为全部配对正确或存在错误配对。

  - `title` string：页面标题，选填；不填或为空时组件显示“请配对连接”。
  - `question`（必填） string：配对任务题干，必须含非空白字符。
  - `options`（必填） array：配对单元，每项是一组正确配对；A 侧在左侧或上方，B 侧在右侧或下方，两侧顺序由组件打乱。

例：

```json
{
 "title": "请配对连接",
 "question": "请将左侧英文单词与右侧图片正确连线。",
 "options": [
  {
   "optionA_text": "Apple",
   "optionA_img": null,
   "optionB_text": "",
   "optionB_img": "https://cdn6-banquan.ituchong.com/weili/sm/2210680063092981771.webp"
  },
  {
   "optionA_text": "Ball",
   "optionA_img": null,
   "optionB_text": "",
   "optionB_img": "https://bpic.51yuansu.com/pic3/cover/03/65/13/5bdd306e90d8d_610.jpg"
  },
  {
   "optionA_text": "Cat",
   "optionA_img": null,
   "optionB_text": "",
   "optionB_img": "https://img1.baidu.com/it/u=2092310685,532760243&fm=253&fmt=auto&app=138&f=JPEG?w=500&h=624"
  },
  {
   "optionA_text": "Dog",
   "optionA_img": null,
   "optionB_text": "",
   "optionB_img": "https://q7.itc.cn/images01/20250703/24ed45d181524ed6bd3f671fa62bbff8.jpeg"
  },
  {
   "optionA_text": "Egg",
   "optionA_img": null,
   "optionB_text": "",
   "optionB_img": "https://img.shetu66.com/2023/12/13/1702438860672478.jpg?x-oss-process=image/resize,h_900/format,webp"
  }
 ]
}
```

### `sort-sentence` 句序排序（0.3.0）

玩家把打乱的短片段点选或拖入作答区，排成唯一正确的顺序后提交；第一次答错可调整后再交一次，第二次仍错则自动演示正确顺序。

  - `title` string：标题，选填；传入时不能为空白。不单独显示，只在没有 question 时作为顶部题干显示。
  - `question` string：顶部题干，选填；传入时不能为空白。未传时显示 title，二者都未传时显示“请将下列内容按正确顺序排列。”。
  - `options`（必填） array：按正确顺序填写的全部待排序片段，组件会打乱展示。判定以本数组顺序为准，玩家必须把全部片段都排入作答区，因此不能混入干扰项。
  - `answer`（必填） string：标准答案文本，必填且不能为空白；写 options 按顺序拼接的文本。参考实现不使用它判定。

例：

```json
{
 "title": "将选项按顺序排序",
 "options": [
  "弹力",
  "是指",
  "发生弹性形变的物体",
  "由于",
  "要恢复原状",
  "对与它接触的物体",
  "产生的作用力"
 ],
 "answer": "弹力是指‌发生弹性形变的物体‌，由于要恢复原状，对与它‌接触的物体‌产生"
}
```

## 现场生成（kind=custom）

`html` 写一个完整、自包含的页面：样式和脚本写在页面里，不联网、不嵌入其他页面、不用表单、不跳转。
引擎会加上统一的样式和安全策略，页面里可以直接用 `.panel`、`.row`、`button.primary`、`.feedback.good/.try/.info`、`.back` 这些样式。
页面要让学生动手（输入、拖动、点击）并给出反馈，结尾提醒学生回到对话里说结果。涉及计算的内容先用 `math_check` 算对。
