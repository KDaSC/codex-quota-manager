# Codex 任务管家界面优化与验收

日期：2026-10-27。范围：原生 macOS 界面；单代理局部修改。

## 本轮改动

- `inbox/Sources/IdeaInboxApp/ControlCenter.swift`：额度面板分为五小时、每周与最终保留线；门槛说明独立展示。聊天列表限制宽度、标题允许两行，增加草稿标记与搜索空状态。详情按聊天、草稿和任务分区。可选任务采用编辑区与已保存列表双栏，展示中文任务状态。
- `inbox/Sources/IdeaInboxApp/App.swift`：收件箱采用统一的系统背景和卡片；内容区滚动，顶部暂停与底部连接状态固定；自动整理选项独立一行；保存使用主按钮样式，附件与文件夹使用次要样式；连接额度注明是上次检查结果。
- 不确定任务增加“核对已完成结果”按钮，调用主聊天新增的 `reconcile_task`。该接口只读核对，按钮禁用与错误展示沿用已有机制。
- `ControlBackend`、`DashboardModel`、`InboxModel` 与修改前备份逐字一致。额度门槛、显式选择、暂停和防重复派发逻辑未因界面调整而更改。
- `.ui-review/` 已加入忽略规则，隔离状态、界面截图和源码备份不纳入源码交付。

## 已执行验证

| 验证 | 结果 |
| --- | --- |
| `swift build -j 1 --package-path inbox` | 编译通过；最终 release 打包也通过 |
| `python3 -m unittest discover -s tests -v` | 221 项，跳过 1 项 Windows 专用检查，其余通过 |
| `node --test tests/test_ui.cjs` | 26 项通过 |
| `node --check codex_resume/static/app.js` | 通过 |
| `swift run -j 1 --package-path inbox idea-inbox-check` | 35/35 通过；使用主聊天修正的模拟服务绝对路径 |
| `inbox/` 中 Python 测试 | 11 项通过 |
| ZIP 在系统临时目录解压后 `codesign --verify --deep --strict` | 通过 |
| ZIP 内后端逐文件对比当前 `codex_resume/` | 一致，包含 `reconcile_task` |

实际窗口使用独立的 `.ui-review/after/inbox` 与 `.ui-review/after/queue`：

1. 看到了真实只读聊天清单、五小时/周额度与保留滑块。
2. 搜索无匹配内容时显示明确提示；选择当前界面聊天可显示详情。
3. 查看了可选任务空列表、未选目标时禁用保存，以及离线样例中的“已保存·未入队”和“发送待核对”按钮。离线样例没有关联实际业务聊天，没有加入执行队列；没有点击核对或派发按钮。
4. 输入并保存两条中文合成想法，其中一条包含 16 段文本；点击保存及 ⌘↩ 均成功；短记录重启后可读回，长记录保存后已读回状态文件。滚动到底部可看到第 16 段和保存位置，顶部暂停与底部连接状态仍可见。
5. 读回 `state.json`，两条均为 `recorded`、`automatic=false`、无 `operation`；队列保持暂停、无 runner。验收应用检查完毕后退出。

截图和完整日志保存在本机 `.ui-review/`。窗口截图为默认 1060×780 点（Retina 图像 2120×1560）。未完成最小窗口尺寸、深色模式及系统辅助功能开关的实际检查；代码继续使用系统语义颜色及原有减少动态/透明度支持。

## 交付

- ZIP：`inbox/应用/Codex任务管家.zip`
- 本机应用：`~/Library/Caches/CodexTaskManagerDevelopment/Codex任务管家.app`
- SHA-256：`544ee7dc93131250db933ac575fa3cbbffa5349b4be7ad906d3a3e58f06b14f7`

工程目录的 File Provider 会重新附加 FinderInfo，直接在工程内解压会导致严格签名检查失败；本轮改在系统临时目录解压原始 ZIP 后验证通过，未为通过检查重新签名。

本轮没有派发用户业务任务，没有执行真实发送测试。离线样例、只读界面检查、此前真实测试聊天发送与自然额度重置后的自动续跑属于不同证据。自然五小时和周额度重置后的完整续跑验收仍未完成。
