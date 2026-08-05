# @tracelens/opencode-plugin（采集插件 · 方案 A）

让 opencode **每次会话空闲**时自动把「模型输出 + 工具调用」导出为标准
**MLflow 格式的 trace.json**，产出文件可直接被 tracelens 处理（inspect / skeleton / expand）。

这是 tracelens「采集」层的 opencode 接入点：opencode 运行时已把每次 LLM 调用、工具调用、
错误完整记录下来，插件负责把它们收敛成 tracelens 认识的标准形态。

## 工作原理

1. 插件注册 `event` 钩子，监听 `session.idle`（每一轮对话完成后触发，携带 `sessionID`）。
2. 通过 opencode SDK（`client.session.messages`）一次拉取该会话的全部消息/部件：
   `text` / `reasoning` / `tool`（含输入、输出、错误）/ `step-finish`（token）。
3. 组装为 MLflow trace：

   | opencode 数据 | span |
   |---|---|
   | 会话 | 根 `AGENT` span |
   | 每个 assistant 消息（一轮） | `LLM` span（inputs=触发它的用户文本，outputs=助手文本+reasoning+tokens） |
   | 该轮内的 `tool` part | `TOOL` span（`state.status==="error"` → `status_code=ERROR` + `status_message`） |

4. 序列化为标准形态 `{"info":…,"data":{"spans":[…]}}` 写入输出目录。

幂等：同一会话 `time.updated` 未变化时不重复写出；多轮时覆盖为更完整版本。
任何导出异常都不会影响会话本身。

## 安装

```bash
cd opencode-plugin
npm install && npm run build        # 产出单文件 dist/plugin.js（零运行时依赖，可即插即用）

# 方式一（推荐）：把单文件放进 opencode 的本地插件目录
#   全局生效：
mkdir -p ~/.config/opencode/plugins
cp dist/plugin.js ~/.config/opencode/plugins/tracelens.js
#   或仅当前项目生效：
mkdir -p .opencode/plugins
cp dist/plugin.js .opencode/plugins/tracelens.js

# 方式二（开发调试，改代码即生效）：符号链接指向构建产物
mkdir -p ~/.config/opencode/plugins
ln -s "$PWD/dist/plugin.js" ~/.config/opencode/plugins/tracelens.js

# 方式三：发布到 npm 后按包名安装
# opencode plugin @tracelens/opencode-plugin
```

安装后启动 opencode，跑完任意会话，即会在输出目录生成 `<会话标题>.trace.json`。

## 配置（环境变量）

| 变量 | 说明 | 默认 |
|---|---|---|
| `TRACELENS_TRACE_DIR` | trace 输出目录 | `<项目>/.tracelens/traces` |
| `TRACELENS_PLUGIN_DISABLED` | `1`/`true` 关闭插件 | 未设置（开启） |

## 使用

```bash
# 会话结束后
tracelens inspect --input .tracelens/traces/会话标题.trace.json
tracelens skeleton --input .tracelens/traces/会话标题.trace.json --format tree --max-tokens 4000
```

## 开发与测试

```bash
npm install
npm run typecheck   # tsc --noEmit
npm run build       # 产出 dist/
npm test            # vitest 单测（builder/serializer）
```

测试要点：
- `tests/fixtures.ts`：模拟多轮 opencode 会话（含 reasoning、正常工具、**工具报错**）。
- `tests/builder.test.ts`：span 结构/父子/状态/错误消息断言。
- `tests/serializer.test.ts`：序列化 schema 断言 + 与提交样本 `tests/fixtures/opencode_sample.trace.json`
  逐字节一致（builder 变更时显式更新样本）。
- 仓库级闭环测试 `../tests/test_opencode_plugin_trace.py`（pytest）：用 tracelens 对固定样本
  做「解析 → 骨架 → 索引 → 按 span_id 逐字节展开」校验，确保插件产出格式与 tracelens 兼容。

## 运行时依赖

插件运行时**零第三方依赖**：只用插件上下文注入的 opencode SDK client 与 Node/Bun 内建模块；
`@opencode-ai/sdk` / `@opencode-ai/plugin` 仅作为类型来源（devDependencies）。
