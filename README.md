# dsh-k12-tutor 0.2.0

K12 学习导师的 DeepSeek Harness bundle。首个学科包是初中数学（人教版，2022 年版课标修订版）。

## 要求

- DeepSeek Harness `>=0.2.1-rc.2 <0.3.0`。
- [uv](https://docs.astral.sh/uv/)。导师的计算和规则引擎是 Python（sympy、pyyaml、jsonschema），第一次使用时 uv 自动准备环境。
  已有装好这三个包的 Python 时，可以设置环境变量 `K12_PYTHON` 指向它，代替 uv。
- 一个支持图片输入的模型（学生常发作业照片），在 profile 的模型设置里配置。bundle 不带模型和凭据。
- `dsh plugin add` 用 pnpm 安装，`PATH` 上要有能用的 pnpm（例如 `npm i -g pnpm`）。

## 安装

建议装进单独的 profile，例如 `k12`：

```bash
dsh --profile k12 --from-default-profile web     # 新建一个 Web profile（启动后可以先关掉）
dsh plugin --profile k12 add <本包的 tarball 路径或 git 地址>
```

再把 `profile.patch.yml` 里需要的行复制进 `<DSH_HOME>/profiles/k12/cordis.patch.yml`：新会话默认使用导师，
禁止导师切换工作目录，在浏览器里选择学生的工作区。新建的 profile 里这个文件只有一个空列表 `[]`，先删掉 `[]` 再粘贴。然后启动：

```bash
dsh --profile k12 --port 3093 --no-open
```

不复制这些行也能用：在新会话的 preset 菜单里选"K12 学习导师"。

## 使用

一个工作区目录就是一个学生。导师把学情档案、会话状态和给家长或老师的危机提醒（`alerts.jsonl`）写在该目录的 `.k12/` 下。
这些数据只在本机，不上传，也不在机器之间同步。`alerts.jsonl` 怎样送到家长或老师手里，需要部署方自己接。

导师会给学生做互动网页（数轴、天平、函数滑块、分步讲解动画、选择填空小测等），写在工作区的 `activities/` 下，
以文件卡片发到对话里，学生点开后在右侧边栏运行。脚本能运行依赖 Web 界面的"Coding Tools"开关（默认开启）。
选择、判断、填空这类小测加载互动方案库的线上组件（`ai-view.new.ndhy.com`），离线时页面改为显示题目文字。

## 许可证

MIT，见 `LICENSE`。原则文件和学科包里引用的研究、课标与教材来源列在 `SOURCES.md`。
