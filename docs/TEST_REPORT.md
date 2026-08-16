# cangjie-knowledge-mcp 测试与改进报告

> 日期:2026-08-17
> 环境:Docker(`cjkb-test` 镜像,Python 3.11.15,pytest 9.1.1)+ 独立 `cangjie-knowledge-mcp` 镜像
> 知识库规模:4185 条 API、237 个示例、3251 条映射、52 个模块

## 1. 测试范围

本次测试覆盖 9 个 MCP 工具、per-call 解析、构造调用提取、降级路径、语义 rerank、错误处理与边界情况,共 **48 项测试**(31 项单元测试 + 17 项综合端到端测试),全部通过。

| 测试类别 | 数量 | 结果 |
|---|---|---|
| 单元测试(tokenize/BM25/parser/searcher/MCP 协议/NL/类型提取/分层检索) | 31 | ✅ 全部通过 |
| 综合端到端(9 工具 + 边界 + rerank) | 17 | ✅ 全部通过 |

## 2. 发现的缺陷与修复

测试发现并修复了 **3 个真实缺陷**:

### 缺陷 1:中文 tokenize 完全失效(严重)

**现象**:`search_api("读取文件所有行")` 返回空;`tokenize("读取文件")` 返回 `[]`。

**根因**:`_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")` 把所有非 ASCII 字符(含中文)都当分隔符切掉,导致中文查询和中文描述在建索引时就被丢弃。重建索引后统计显示旧索引中 **CJK token 数量为 0**。

**修复**:`_TOKEN_SPLIT` 改为 `[^A-Za-z0-9\u4e00-\u9fff]+`(保留中文),并对纯 CJK 段做 **unigram(单字)切分**——中文无空格词边界,单字切分让 BM25 能基于字重叠打分,无需引入分词器。

**效果**:重建索引后中文检索显著改善,如 `读写锁` 精确命中 `ReadWriteLock`,`线程` 命中 `ThreadLocal/sleep/Future`。

### 缺陷 2:菱形构造调用 `new HashMap<>()` 未被提取

**现象**:`HashMap<String,Integer> map = new HashMap<>();` 只提取到 `map.put`/`map.get`,漏掉构造调用。

**根因**:`_NEW_CALL_RE` 的泛型部分是 `<[^(){};]+>?`,要求至少一个字符,空的菱形 `<>` 不匹配。

**修复**:改为 `<[^(){};]*>?`(允许空泛型)。修复后 `new HashMap<>()` 正确被提取为构造调用。

### 缺陷 3:类型锁定对带泛型类型跑偏

**现象**:`_lock_types("HashMap<String,Integer>")` 返回 `RawStatsReporter`/`OptionInfo` 等无关类型;`new HashMap()` 锁到 `ConcurrentHashMap` 而非 `HashMap`。

**根因**:两处——(1) `_lock_types` 对无点号的泛型串没剥泛型,`HashMap<String,Integer>` 直接拿去查表和 BM25 搜;(2) `_call_api_suggest` 用带泛型的 `declared_type`(如 `HashMap<>`)锁类型,导致映射表精确查询失败、落入噪声的 BM25 类名搜索。

**修复**:(1) `_lock_types` 先 `re.sub(r"<.*>", "", jt)` 剥泛型再取 simple name;(2) `_call_api_suggest` 改用已剥泛型的 `declared_simple` 锁类型。

**效果**:`map.put`/`map.get`/`new HashMap()` 三个调用全部正确锁定 `HashMap @ std.collection`。

## 3. 语义 rerank 增强层(已有,本次回归验证)

rerank 层(LLM 语义重排,LongCodeZip 思路)在本次测试中通过回归验证:
- **回退正确性**:`rerank=false` 或无 api_key 时,顺序与纯 BM25 完全一致(检索永不因 LLM 失败而退化)。
- **解析健壮性**:`_parse_order` 对垃圾输入、越界索引、重复索引、单元素数组均正确返回 None(回退 BM25)。
- **推理模型适配**:`deepseek-v4-flash` 是推理模型,`content` 字段在 `max_tokens` 不足时为空;已通过 `max_tokens=4096` + 回退解析 `reasoning_content` 解决。

## 4. opencode 集成

- 新增 **`opencode.json`**(项目级配置):注册 MCP server,通过 Docker 镜像 `cangjie-knowledge-mcp` 启动(宿主机无 Python,容器内运行)。
- 新增 **`Dockerfile`**:基于 `python:3.11-slim`,打包源码 + 知识库 JSONL + PyYAML,`ENTRYPOINT` 为 MCP stdio server。
- 验证:镜像能正确响应 MCP `initialize`、`tools/list`、`tools/call`。

## 5. 安全处理

- **config.yaml 明文 API key**:已从 config.yaml 移除,改为空字符串,依赖 `OPENAI_API_KEY` 环境变量注入(经确认该 key 未进入 git 历史)。
- **机器专用绝对路径**:config.yaml 中 `j2cjlib`/`java_terms` 恢复为 `<x2cangjie路径>` 占位符。
- **.gitignore**:新增 `corpus/`(341MB 本地语料克隆)和 `.sisyphus/`(工具状态)规则。

## 6. 结论

所有 48 项测试通过,3 个真实缺陷已修复,opencode 集成与安全处理完成。项目处于可提交状态。
